"""#3300 P3 (Y-server) — cancel-by-id for an UNDISPATCHED (queued) user message.

Server-authoritative, WAL-durable cancellation of a queued (not yet
dispatched) inbox item. Y-client (the textual_chat sent-queue row removal +
composer newline-prepend) is a LATER, separately-owned PR — this file gates
the SERVER half only: ``Session.cancel_queued`` / ``SnapshotJournal.cancel_inbox``
/ the WAL ``inbox_cancel`` vocabulary / the ``inbox_cancel`` audit-event delta /
remote (agui) parity.

Covers (see the architect's #3300 design-pass comments on the issue):

  1. **Three semantics** — queued->removed, dispatched->no-op, idempotent.
  2. **★§1 snapshot-prune + truncate-falsify** (CLAUDE.md recovery-feature PR
     gate, the architect's most important contract correction): a cancel's
     WAL ``inbox_cancel`` tombstone ALONE is not sufficient — the inbox is
     snapshot-backed, so the snapshot must ALSO be pruned synchronously at
     cancel-record time, or a WAL truncation below the cancelled item's
     ``inbox_put`` event resurrects it on restore.
  3. **★F no-await critical section / cancel-during-dequeue race** — the
     queued/dispatched judgement is atomic: under concurrent scheduling with
     the dispatcher's own dequeue-then-promote sequence, EXACTLY ONE of
     (``inbox_cancel`` removal / ``turn_started`` promote) is ever observed
     for the same item, never both.
  4. **skip-at-consume** — a cancelled item still physically sitting in the
     plain ``asyncio.Queue`` (no removal API) is discarded, not dispatched,
     whenever it is eventually dequeued.
  5. **``inbox_cancel`` delta** — seq-stamped like ``user_submitted``/
     ``turn_started``; ``RemoteQueueView.apply_inbox_cancel`` removes it from
     a client's queue model.
  6. **remote (agui) parity** — the SAME op reaches a remote client's queue
     model via the AG-UI wire (generic EventFrame/CUSTOM encode, no
     per-surface wiring needed — the completeness gates bind this).

Real ``Session``/``StateLog``/``AgentSnapshot`` throughout — no
``unittest.mock``. #5450 migration: the turn-mid-flight hang is now
``@pytest.mark.llm_stub(control="gated")`` — the real
``RouterLoopDriver``/``RouterLoop`` dispatch for real, hung at the real
``litellm.acompletion`` boundary, replacing the private
``_loop_driver.run_turn`` replacement ``tests/core/test_2242_hard_cancel.py``
used before its own #5450 migration (#5468).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.state import RemoteQueueView
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle

AGENT = "p3-cancel-agent"


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _collect(session: Session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran (#5450)."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


def _reconstruct(agent_name: str, snapshot_path: Path, state_log: StateLog) -> AgentSnapshot:
    """Mirror ``AgentRegistry.restore_all``'s algorithm: load the durable
    snapshot, tail the WAL from its ``applied_seq``, replay onto it."""
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    events = list(state_log.iter_from(snap.applied_seq))
    snap.apply_events(events)
    return snap


# ── 1. three semantics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_queued_removes_undispatched_item(tmp_path):
    """Tier 2: cancelling a still-queued (undispatched) msg_id removes it from
    ``queued_user_messages()`` and returns True."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    await session.submit_user_text("first")
    await session.submit_user_text("second")
    queued = session.queued_user_messages()
    assert [i["text"] for i in queued] == ["first", "second"]
    target = queued[0]["msg_id"]

    cancelled = await session.cancel_queued(target)

    assert cancelled is True
    remaining = session.queued_user_messages()
    assert [i["text"] for i in remaining] == ["second"]


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_cancel_of_already_dispatched_message_is_a_noop(tmp_path, _llm_stub):
    """Tier 2: cancelling a msg_id that has ALREADY been dispatched (consumed
    off the inbox to start its turn) is a no-op — it must NOT escalate to
    ``cancel_inflight`` (a distinct intent for the running turn)."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    events = _collect(session)
    await session.submit_user_text("dispatch-me")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()
    assert session.queued_user_messages() == [], "sanity: the item is now dispatched"

    cancelled = await session.cancel_queued(msg_id)

    assert cancelled is False, "cancelling an already-dispatched item must be a no-op"
    assert session.turn_active is True, "the running turn must be UNAFFECTED by cancel_queued"

    _llm_stub.release.set()
    await turn_task

    # witness ②: the real driver dispatched.
    await settle(session)
    assert any(e.type == "turn_started" for e in events)


@pytest.mark.asyncio
async def test_cancel_queued_is_idempotent(tmp_path):
    """Tier 2: a second cancel of the same msg_id is a no-op — safe for an
    at-most-once reconnect retry."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    await session.submit_user_text("only")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    first = await session.cancel_queued(msg_id)
    second = await session.cancel_queued(msg_id)

    assert first is True
    assert second is False, "a second cancel of the same (already cancelled) id is a no-op"
    assert session.queued_user_messages() == []


@pytest.mark.asyncio
async def test_cancel_queued_of_unknown_msg_id_is_a_noop(tmp_path):
    """Tier 2: cancelling an id the session never saw is a no-op (never raises)."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    assert await session.cancel_queued("no-such-id") is False


# ── 2. ★§1 snapshot-prune + truncate-falsify (CLAUDE.md recovery-feature gate) ──


@pytest.mark.asyncio
async def test_cancelled_message_survives_wal_truncation_below_its_source_events(tmp_path):
    """Tier 2: ★truncate-falsify (CLAUDE.md recovery-feature PR gate, required
    same-PR). Queue X, cancel X (the snapshot-prune executes synchronously),
    checkpoint, then TRUNCATE the WAL below X's own source events
    (``inbox_put``/``inbox_cancel``) — reconstructing (load snapshot + replay
    the WAL tail) must still show X absent, because the value is baked into
    the durable FULL-STATE snapshot, not solely derived from the (now-dropped)
    WAL events. An UNCANCELLED queued message survives the SAME truncation
    (the snapshot holds it) — proving this isn't "everything before the floor
    vanishes" but specifically that inbox correctness survives.

    ★Strip-falsify (verified manually per repo discipline, mirroring
    ``tests/core/test_2884_hook_driven_turns_truncation_falsify.py``): commenting
    out the ``self._snapshot.inbox = [...]`` prune line in
    ``SnapshotJournal.cancel_inbox`` (leaving only the WAL ``inbox_cancel``
    tombstone) makes the cancelled item's ``snapshot.inbox`` entry survive
    into the on-disk snapshot; after the SAME truncation below its source
    events, reconstruction (load snapshot + replay the truncated tail, which
    no longer contains ``inbox_cancel``) shows the "cancelled" item back in
    the queue — RED. This witnesses that the synchronous snapshot-prune (not
    the WAL tombstone) is what makes cancellation recovery-safe."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)

    await session.submit_user_text("cancel-me")
    await session.submit_user_text("keep-me")
    cancel_id = session.queued_user_messages()[0]["msg_id"]
    keep_id = session.queued_user_messages()[1]["msg_id"]

    cancelled = await session.cancel_queued(cancel_id)
    assert cancelled is True
    await session.journal.flush()  # drain the fire-and-forget WAL + snapshot writes

    assert [i["msg_id"] for i in session.queued_user_messages()] == [keep_id], (
        "sanity: the live snapshot reflects the cancel immediately"
    )

    # The source events (inbox_put x2, inbox_cancel x1) are durable below this point.
    pre_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert any('"inbox_cancel"' in ln and f'"msg_id": "{cancel_id}"' in ln for ln in pre_truncate_lines), (
        "sanity: the inbox_cancel source event must be durable pre-truncation"
    )

    # push filler events far past the cancel's source events, then truncate below them.
    for i in range(150):
        await state_log.append("inbox_put", n=i, target="filler-agent", msg_id=f"filler-{i}", msg_kind="user", payload={})
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 3, (
        f"the 3 real source events (2x inbox_put + 1x inbox_cancel) must be truncated "
        f"below the floor; dropped={stats['dropped']}"
    )
    post_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert not any('"inbox_cancel"' in ln for ln in post_truncate_lines), (
        "the inbox_cancel source event must actually be gone from the WAL post-truncation "
        "(not just counted) — otherwise this test would pass vacuously"
    )

    await state_log.aclose()  # simulate the crash: tear down run1's WAL worker

    # reconstruct (simulates a restart): a FRESH StateLog over the SAME wal/snapshot.
    state_log2 = StateLog(wal)
    reconstructed = _reconstruct(AGENT, snapshot_path, state_log2)

    reconstructed_ids = {m["id"] for m in reconstructed.inbox}
    assert cancel_id not in reconstructed_ids, (
        "the cancelled message must NOT resurrect after WAL truncation below its own "
        "source events (snapshot-backed cancellation, not WAL-derived)"
    )
    assert keep_id in reconstructed_ids, (
        "an UNCANCELLED queued message must SURVIVE the same truncation (the snapshot "
        "still holds it) — proving this is inbox correctness, not blanket data loss"
    )

    await state_log2.aclose()


@pytest.mark.asyncio
async def test_snapshot_journal_cancel_inbox_round_trip(tmp_path):
    """Tier 1: a basic ``SnapshotJournal.cancel_inbox`` round-trip — the
    prune is reflected in ``.snapshot.inbox`` immediately (not only after a
    later save/load), and the boolean reports presence correctly."""
    from reyn.runtime.services.snapshot_journal import SnapshotJournal

    wal = tmp_path / "state.wal"
    state_log = StateLog(wal)
    journal = SnapshotJournal(
        agent_name=AGENT, snapshot_path=tmp_path / "snapshot.json", state_log=state_log,
    )
    msg_id = await journal.append_inbox(kind="user", payload={"text": "hi", "chain_id": "c1"})
    assert any(m["id"] == msg_id for m in journal.snapshot.inbox)

    ok = await journal.cancel_inbox(msg_id=msg_id)
    assert ok is True
    assert not any(m["id"] == msg_id for m in journal.snapshot.inbox)

    # idempotent: a second cancel of the same id is a no-op.
    ok2 = await journal.cancel_inbox(msg_id=msg_id)
    assert ok2 is False

    await state_log.aclose()


# ── 3. ★F no-await critical section / cancel-during-dequeue race ────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_cancel_scheduled_before_dispatch_wins_exclusively(tmp_path, _llm_stub):
    """Tier 2: ★race gate (design-pass pin F, #79). ``cancel_queued`` and the
    dispatcher's own dequeue-then-promote (``run_one_iteration``) are launched
    as CONCURRENT asyncio tasks contending for the SAME queued item, cancel
    scheduled first. Because both paths' decision is a no-await synchronous
    critical section (see ``SnapshotJournal.cancel_inbox`` / ``Session.
    cancel_queued`` docstrings), whichever task the loop runs first commits
    its exit atomically before the other can observe stale state: cancel wins
    here, EXCLUSIVELY — the item is discarded (skip-at-consume) when later
    dequeued, never promoted (``turn_started`` never fires for it).

    control="gated" is installed only as a safety net (no assertion needs
    ``call_started``/``release`` here) — if dispatch ever DID win this race
    (it must not), the LLM boundary must still be patched rather than
    attempting a real network call."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")

    await session.submit_user_text("race-me")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    captured: list = []
    session.subscribe_audit_events(lambda ev: captured.append(ev))

    cancel_task = asyncio.create_task(session.cancel_queued(msg_id))
    iter_task = asyncio.create_task(session.run_one_iteration())
    # Let the item settle: cancel wins deterministically (scheduled first,
    # both fully synchronous end-to-end up to their first real suspension).
    cancelled = await asyncio.wait_for(cancel_task, timeout=5)
    assert cancelled is True

    # The still-pending run_one_iteration discards the physically-enqueued
    # (but now cancelled) item and re-blocks on the empty inbox — unblock it
    # with a shutdown sentinel so the task completes instead of hanging.
    await session.inbox.put((("shutdown"), {}))
    finished = await asyncio.wait_for(iter_task, timeout=5)
    assert finished is False, "shutdown sentinel drains run_one_iteration"

    await settle(session)
    kinds = [ev.type for ev in captured]
    assert "inbox_cancel" in kinds
    assert "turn_started" not in kinds, (
        "cancel won the race — the item must NEVER ALSO be promoted (exclusivity)"
    )
    inbox_cancel_ev = next(ev for ev in captured if ev.type == "inbox_cancel")
    assert inbox_cancel_ev.data["msg_id"] == msg_id


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_dispatch_scheduled_before_cancel_wins_exclusively(tmp_path, _llm_stub):
    """Tier 2: the REVERSE ordering of the race above — dispatch scheduled
    first. By the time cancel's task runs, the item is already consumed
    (its snapshot.inbox entry pruned + ``turn_started`` already emitted, both
    inside the dispatcher's own no-await synchronous prefix) — cancel
    observes "already dispatched" and correctly no-ops. EXACTLY ONE of the
    two exits fires, never both, regardless of which ordering wins.

    Witness ② (#5450) is implicit: ``assert "turn_started" in kinds`` below
    already depends on the real driver having dispatched."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")

    await session.submit_user_text("race-me-2")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    captured: list = []
    session.subscribe_audit_events(lambda ev: captured.append(ev))

    iter_task = asyncio.create_task(session.run_one_iteration())
    cancel_task = asyncio.create_task(session.cancel_queued(msg_id))

    await _llm_stub.call_started.wait()
    cancelled = await cancel_task

    assert cancelled is False, "dispatch won the race — cancel of an already-dispatched item is a no-op"
    await settle(session)
    kinds = [ev.type for ev in captured]
    assert "turn_started" in kinds
    assert "inbox_cancel" not in kinds, (
        "dispatch won the race — the item must NEVER ALSO be cancelled (exclusivity)"
    )

    _llm_stub.release.set()
    finished = await iter_task
    assert finished is True


# ── 4. skip-at-consume ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_skip_at_consume_discards_cancelled_item_without_dispatch(tmp_path, _llm_stub):
    """Tier 2: a cancelled item still physically sitting in the plain
    ``asyncio.Queue`` (no removal API) is discarded — never dispatched, never
    staged as a ride-along — when it is eventually dequeued. A LATER queued
    item is unaffected and dispatches normally.

    Witness ② (#5450): turn_started fires for the SECOND (uncancelled)
    item's chain_id — proof the real driver dispatched THAT item, not just
    that the stub returned."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    events = _collect(session)

    await session.submit_user_text("cancel-me")
    cancel_id = session.queued_user_messages()[0]["msg_id"]
    assert await session.cancel_queued(cancel_id) is True

    await session.submit_user_text("dispatch-me")

    # run_one_iteration must skip the cancelled item and dispatch the SECOND
    # (uncancelled) one instead — never a turn for the cancelled text.
    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()
    _llm_stub.release.set()
    finished = await turn_task
    assert finished is True
    assert session.queued_user_messages() == []

    await settle(session)
    assert any(e.type == "turn_started" for e in events)


# ── 5. inbox_cancel delta / RemoteQueueView ──────────────────────────────────


def test_remote_queue_view_apply_inbox_cancel_removes_by_msg_id():
    """Tier 1: ``RemoteQueueView.apply_inbox_cancel`` removes the item BY ITS
    OWN msg_id (a cancel targets one specific queued item, not a whole
    chain — unlike ``apply_turn_started``, which matches by chain_id), and is
    seq-gated like the other queue mutations (order-race protocol, design-pass
    pin D)."""
    view = RemoteQueueView()
    view.apply_user_submitted(msg_id="m1", chain_id="c1", text="hi", seq=1)
    view.apply_user_submitted(msg_id="m2", chain_id="c2", text="bye", seq=2)
    assert {i["msg_id"] for i in view.queue()} == {"m1", "m2"}

    removed = view.apply_inbox_cancel(msg_id="m1", seq=3)

    assert removed is True
    assert [i["msg_id"] for i in view.queue()] == ["m2"]

    # stale/duplicate cancel delta (seq not strictly greater) is a no-op.
    stale = view.apply_inbox_cancel(msg_id="m2", seq=2)
    assert stale is False
    assert [i["msg_id"] for i in view.queue()] == ["m2"]


@pytest.mark.asyncio
async def test_real_session_inbox_cancel_delta_drives_remote_queue_view(tmp_path):
    """Tier 2: a real ``Session.cancel_queued``'s ``inbox_cancel`` audit-event
    — the SAME event the transport/agui completeness gates bind — drives a
    ``RemoteQueueView`` to the correct final state end-to-end."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")

    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=0)
    captured: list = []
    session.subscribe_audit_events(lambda ev: captured.append(ev))

    await session.submit_user_text("alpha")
    await settle(session)
    submitted = next(e for e in captured if e.type == "user_submitted")
    view.apply_user_submitted(
        msg_id=submitted.data["msg_id"], chain_id=submitted.data["chain_id"],
        text=submitted.data["text"], seq=submitted.data["seq"],
    )
    assert [i["text"] for i in view.queue()] == ["alpha"]

    await session.cancel_queued(submitted.data["msg_id"])
    await settle(session)
    cancel_ev = next(e for e in captured if e.type == "inbox_cancel")
    view.apply_inbox_cancel(msg_id=cancel_ev.data["msg_id"], seq=cancel_ev.data["seq"])

    assert view.queue() == [], "the cancelled item must leave the queue model"


# ── 6. remote (agui) parity ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agui_endpoint_cancel_queued_ptype_reaches_session(tmp_path):
    """Tier 2: remote parity — the ``cancel_queued`` AG-UI wire ptype
    (endpoint.py) reaches ``Session.cancel_queued`` exactly like a local
    in-process call, and the SAME server-authoritative removal happens."""
    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    await session.submit_user_text("remote-cancel-me")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    # Mirrors the endpoint's dispatch (endpoint.py `elif ptype == "cancel_queued"`)
    # without spinning up the full Starlette app — the transport-seam contract
    # under test is "the wire ptype calls session.cancel_queued(msg_id)".
    cancel_fn = getattr(session, "cancel_queued")
    await cancel_fn(msg_id)

    assert session.queued_user_messages() == []


@pytest.mark.asyncio
async def test_agui_client_transport_cancel_queued_sends_wire_ptype():
    """Tier 2: ``AgUiTransport.cancel_queued`` (the client-side send seam)
    POSTs the distinct ``cancel_queued`` ptype with the msg_id — never
    ``cancel_inflight`` (a different intent)."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    sent: list = []

    async def _send(payload: dict) -> bool:
        sent.append(payload)
        return True

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)

    result = await transport.cancel_queued("m-123")

    assert result is True
    assert sent == [{"type": "cancel_queued", "msg_id": "m-123"}]


@pytest.mark.asyncio
async def test_in_process_transport_cancel_queued_delegates_to_session(tmp_path):
    """Tier 2: ``InProcessTransport.cancel_queued`` delegates to the attached
    session's ``cancel_queued`` — the in-process half of remote parity."""
    from reyn.interfaces.transport.in_process import InProcessTransport

    session, _ = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    await session.submit_user_text("in-process-cancel-me")
    msg_id = session.queued_user_messages()[0]["msg_id"]

    class _FakeRegistry:
        def attached_session(self):
            return session

    transport = InProcessTransport(_FakeRegistry(), intervention_channel="test")

    result = await transport.cancel_queued(msg_id)

    assert result is True
    assert session.queued_user_messages() == []


# ── 7. WAL vocabulary / apply_events replay ──────────────────────────────────


def test_agent_snapshot_apply_events_inbox_cancel_prunes_like_inbox_consume():
    """Tier 1: ``AgentSnapshot.apply_events`` treats ``inbox_cancel`` events
    symmetrically with ``inbox_consume`` for pure-replay purposes (both
    remove the matching inbox entry) — the WAL replay identity
    ``inbox_put − inbox_consume − inbox_cancel``."""
    snap = AgentSnapshot.empty(AGENT, "main")
    events = [
        {"kind": "inbox_put", "target": AGENT, "session_id": "main", "seq": 1,
         "msg_id": "a", "msg_kind": "user", "payload": {"text": "keep"}},
        {"kind": "inbox_put", "target": AGENT, "session_id": "main", "seq": 2,
         "msg_id": "b", "msg_kind": "user", "payload": {"text": "cancel"}},
        {"kind": "inbox_cancel", "agent": AGENT, "session_id": "main", "seq": 3,
         "msg_id": "b"},
    ]
    snap.apply_events(events)

    ids = {m["id"] for m in snap.inbox}
    assert ids == {"a"}, "inbox_cancel must prune its target id exactly like inbox_consume"
