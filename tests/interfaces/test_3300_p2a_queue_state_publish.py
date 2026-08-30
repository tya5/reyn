"""#3300 P2a — publish server-authoritative sent-queue + turn-active state.

Phase 2a of the input-message-lifecycle arc: make the server-authoritative
queue state (the current UNDISPATCHED inbox items + whether a turn is
dispatched) knowable to a client via snapshot + deltas — never derived by a
client guessing from a partial event stream it may have joined mid-turn
(the late-joiner hazard). Rendering that state (the sent-queue widget) is
P2b, out of scope here — this file gates the TRANSPORT/STATE layer only.

Covers (see the architect's #3300 design-pass comments on the issue):

  1. **turn-active accessor** (additive) — ``Session.turn_active`` exposes the
     existing ``_turn_idle`` event as a public read, not a new authority.
  2. **queued_user_messages()** — ``Session``'s read-only accessor for the
     current undispatched ``kind=="user"`` inbox queue (snapshot-backed via
     ``SnapshotJournal``, same durable state ``append_inbox``/``consume_inbox``
     already keep current).
  3. **remote parity** — the same queue+turn-active values reach a remote
     (agui) client via ``STATE_SNAPSHOT``/``STATE_DELTA`` as a local
     (in-process, direct accessor read) client sees.
  4. **late-joiner-safe (non-vacuity)** — a client "connecting" DURING a turn
     (having missed the ``turn_started`` that dispatched the in-flight item)
     reconstructs the CORRECT queue + turn-active from the snapshot alone.
  5. **order-race gate (design-pass pin D)** — ``RemoteQueueView``'s seq-gated
     merge of the granular ``user_submitted``/``turn_started`` deltas is safe
     under ANY interleaving with a snapshot read: no duplicate, no
     resurrection-after-dispatch, no loss.
  6. **deltas keep the model in sync** — a real session's ``user_submitted``
     (enqueue) and ``turn_started`` (dispatch) audit-events, consumed through
     ``RemoteQueueView``, produce an accurate queue model end-to-end.

Real ``Session``/``AgentRegistry``/``StateLog`` throughout — no
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

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.state import RemoteQueueView
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle

AGENT = "p2a-queue-agent"


def _make_session(wal: Path, snapshot_path: Path, *, agent_name: str = AGENT) -> Session:
    state_log = StateLog(wal)
    return make_session(agent_name=agent_name, state_log=state_log, snapshot_path=snapshot_path)


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "state.wal")

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _stop_auto_driver(registry: AgentRegistry, name: str) -> None:
    """``AgentRegistry.attach`` boots a background ``session.run()`` driver
    loop that would otherwise race a test's own manual
    ``run_one_iteration()`` calls (both dequeuing from the same inbox). Cancel
    it so the test has sole, deterministic control over dispatch — mirrors
    how ``tests/core/test_2242_hard_cancel.py`` drives a bare (non-registry)
    ``Session`` directly; this is the registry-attached equivalent."""
    key = (name, "main")
    task = registry._tasks.get(key)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    fwd = registry._forward_tasks.get(key)
    if fwd is not None and not fwd.done():
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


def _collect(session: Session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran (#5450)."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _connect_snapshot_only(status_provider) -> "dict":
    """Simulate a client connecting: build an AgUiEmitter whose live-frame
    stream ends immediately, so the ONLY thing the wire carries is the
    connect-time STATE_SNAPSHOT (+ the empty MESSAGES_SNAPSHOT) — exactly
    what a late-joining remote client receives before any live frame."""
    async def frames():
        return
        yield  # pragma: no cover - makes this an async generator

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass  # draining applies STATE_SNAPSHOT to transport.status
    return dict(transport.status.values)


# ── 1. turn-active accessor (additive) ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_turn_active_accessor_reflects_busy_then_idle(tmp_path, _llm_stub):
    """Tier 2: ``Session.turn_active`` is False when idle, True once a turn is
    dispatched (mid-flight), and False again once it settles — an additive
    read of the existing ``_turn_idle`` event, not a new authority.

    Witness ② (#5450): turn_started fires — proof the REAL driver
    dispatched, not merely that the stub was called."""
    session = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    events = _collect(session)
    assert session.turn_active is False

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    turn_task = asyncio.create_task(session.run_one_iteration())

    await _llm_stub.call_started.wait()
    assert session.turn_active is True

    _llm_stub.release.set()
    completed = await turn_task
    assert completed is True
    assert session.turn_active is False

    await settle(session)
    assert any(e.type == "turn_started" and e.data.get("chain_id") == "c1" for e in events)


# ── 2. queued_user_messages() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_user_messages_reflects_undispatched_inbox_queue(tmp_path):
    """Tier 2: ``Session.queued_user_messages()`` lists every UNDISPATCHED
    ``kind=="user"`` inbox item (snapshot-backed) and drops an item the
    instant it is consumed (dispatched) — no dependency on the turn actually
    completing."""
    session = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")
    assert session.queued_user_messages() == []

    await session.submit_user_text("first")
    await session.submit_user_text("second")

    queued = session.queued_user_messages()
    assert [item["text"] for item in queued] == ["first", "second"]
    assert all(item["msg_id"] and item["chain_id"] for item in queued)

    # Dispatch (consume) the first item directly — mirrors what
    # ``run_one_iteration`` does before the turn body runs.
    await session._inbox_arbiter.consume_inbox()
    remaining = session.queued_user_messages()
    assert [item["text"] for item in remaining] == ["second"]


# ── 3. remote parity (local ≡ remote) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_remote_parity_state_snapshot_matches_local_accessors(tmp_path):
    """Tier 2: the STATE_SNAPSHOT a remote (agui) client receives carries the
    SAME queue + turn_active values the local in-process accessors return —
    local ≡ remote by construction (both derive from the identical
    ``_snapshot(registry)`` read-model)."""
    registry = _make_registry(tmp_path)
    _seed(tmp_path, AGENT)
    session = await registry.attach(AGENT)
    await _stop_auto_driver(registry, AGENT)

    await session.submit_user_text("remote parity check")

    local_queue = session.queued_user_messages()
    local_turn_active = session.turn_active

    values = await _connect_snapshot_only(lambda: _snapshot(registry))

    assert values.get("queue") == local_queue
    assert values.get("turn_active") == local_turn_active is False


# ── 4. late-joiner-safe (non-vacuity) ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_late_joiner_mid_turn_connect_reconstructs_correct_state(tmp_path, _llm_stub):
    """Tier 2: #3300 P2a core correctness property. A client "connecting"
    DURING a turn — having missed the ``turn_started`` audit-event that
    dispatched the in-flight item — still reconstructs the CORRECT
    queue (the second, still-undispatched item) + turn-active=True from the
    STATE_SNAPSHOT alone.

    Non-vacuity / strip-falsify (verified manually per repo discipline,
    Edit-to-break -> Edit-to-restore in ``interfaces/repl/status.py``): commenting
    out the ``"queue"``/``"turn_active"`` keys in ``_snapshot()`` makes the
    assertions below fail — a late-joining client would derive an EMPTY queue
    and idle turn-active despite a real in-flight turn + a real queued item,
    exactly the black-hole/mis-derivation this gate exists to catch.
    """
    registry = _make_registry(tmp_path)
    _seed(tmp_path, AGENT)
    session = await registry.attach(AGENT)
    await _stop_auto_driver(registry, AGENT)
    events = _collect(session)

    await session.submit_user_text("dispatched-first")
    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()

    # A second submission arrives WHILE the first turn is still in flight —
    # it stays undispatched (server-authoritative queue, #3300 design-pass
    # §6b: not a black-hole, durably queued).
    await session.submit_user_text("queued-second")

    # The "late joiner": connects only NOW, having missed the turn_started
    # for "dispatched-first" entirely (it happened before this connect).
    values = await _connect_snapshot_only(lambda: _snapshot(registry))

    assert values.get("turn_active") is True, (
        "a client connecting mid-turn must see turn_active=True from the "
        "snapshot alone, never derive idle from a missed turn_started"
    )
    queue_texts = [item["text"] for item in values.get("queue", [])]
    assert queue_texts == ["queued-second"], (
        "the late joiner must see exactly the still-undispatched item, not "
        "the dispatched one and not an empty queue"
    )

    _llm_stub.release.set()
    await turn_task

    # After the turn settles, a fresh connect reflects idle + still-queued.
    values_after = await _connect_snapshot_only(lambda: _snapshot(registry))
    assert values_after.get("turn_active") is False
    assert [item["text"] for item in values_after.get("queue", [])] == ["queued-second"]

    # witness ②: the real driver dispatched "dispatched-first" — exactly one
    # turn_started (the second submission stays queued, undispatched).
    await settle(session)
    (started_ev,) = [e for e in events if e.type == "turn_started"]
    assert started_ev.data.get("kind") == "user"


# ── 5. order-race gate (design-pass pin D) ───────────────────────────────────


def test_remote_queue_view_seq_gate_prevents_resurrection_after_dispatch():
    """Tier 1: ``RemoteQueueView``'s seq-gate resolves the snapshot/delta
    order-race: a client that reads a STATE_SNAPSHOT taken AFTER an item was
    dispatched (so the snapshot's queue no longer contains it) must not let a
    stale/out-of-order ``user_submitted`` delta for that SAME item resurrect
    it — regardless of the relative arrival order.

    This assertion is load-bearing on the seq-gate line itself: removing the
    ``if seq <= self._last_seq: return False`` guard in
    ``RemoteQueueView.apply_user_submitted`` makes the stale delta re-add the
    item and this test goes RED (verified manually per repo discipline,
    Edit-to-break -> Edit-to-restore in ``interfaces/transport/agui/state.py``).
    """
    view = RemoteQueueView()

    # Server-side history: item "m1" (chain "c1") was enqueued at seq=1, then
    # dispatched at seq=2. A snapshot taken AFTER the dispatch reflects an
    # empty queue with queue_seq=2.
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=2)
    assert view.queue() == []

    # A stale/duplicate "user_submitted" delta for the ALREADY-DISPATCHED
    # item arrives AFTER the snapshot (out-of-order delivery / replay).
    applied = view.apply_user_submitted(msg_id="m1", chain_id="c1", text="hi", seq=1)
    assert applied is False, "a delta whose seq <= the snapshot's queue_seq must be a no-op"
    assert view.queue() == [], "the dispatched item must NOT resurrect"

    # The reverse interleaving (delta arrives BEFORE any snapshot) still
    # works: a genuinely new enqueue (higher seq than anything seen) IS
    # applied — no loss.
    fresh = RemoteQueueView()
    applied2 = fresh.apply_user_submitted(msg_id="m2", chain_id="c2", text="new", seq=1)
    assert applied2 is True
    assert fresh.queue() == [{"msg_id": "m2", "chain_id": "c2", "text": "new"}]

    # A duplicate delivery of the SAME delta (seq unchanged) is a no-op — no
    # duplicate entries / no double-processing.
    applied3 = fresh.apply_user_submitted(msg_id="m2", chain_id="c2", text="new", seq=1)
    assert applied3 is False
    assert fresh.queue() == [{"msg_id": "m2", "chain_id": "c2", "text": "new"}]

    # A genuinely later dispatch (turn_started) DOES remove it — no loss of
    # the legitimate promote-out-of-queue transition.
    removed = fresh.apply_turn_started(chain_id="c2", seq=2)
    assert removed is True
    assert fresh.queue() == []


def test_remote_queue_view_snapshot_mid_stream_stays_consistent_with_redelivery():
    """Tier 1: a connection's SSE stream preserves per-connection delivery
    order (a single ordered queue, #3300 P2a emitter), so the realistic race
    is the SNAPSHOT read landing at an arbitrary point relative to an
    in-order delta stream — including the snapshot's own subscription window
    re-delivering a delta the snapshot ALREADY reflects (buffering overlap on
    (re)connect). Both must resolve correctly: the in-order deltas apply, and
    the redelivered already-reflected one is a no-op (no duplicate)."""
    view = RemoteQueueView()

    # m1 enqueued (seq=1); a snapshot taken right after already reflects it.
    view.apply_snapshot(queue=[{"msg_id": "m1", "chain_id": "c1", "text": "a"}],
                         turn_active=True, queue_seq=1)

    # The subscription's buffering window re-delivers that SAME seq=1 enqueue
    # (connect-time overlap) — already reflected by the snapshot, a no-op.
    redelivered = view.apply_user_submitted(msg_id="m1", chain_id="c1", text="a", seq=1)
    assert redelivered is False
    assert view.queue() == [{"msg_id": "m1", "chain_id": "c1", "text": "a"}]

    # Subsequent, genuinely new, in-order deltas apply normally: m2 enqueues
    # (seq=2), then m1 dispatches (seq=3).
    assert view.apply_user_submitted(msg_id="m2", chain_id="c2", text="b", seq=2) is True
    assert view.apply_turn_started(chain_id="c1", seq=3) is True

    assert view.queue() == [{"msg_id": "m2", "chain_id": "c2", "text": "b"}]


# ── 6. deltas keep the model in sync (real session, end-to-end) ─────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_real_session_deltas_keep_remote_queue_view_accurate(tmp_path, _llm_stub):
    """Tier 2: a real ``Session``'s ``user_submitted`` (enqueue) and
    ``turn_started`` (dispatch) audit-events — the SAME events P1 (C) already
    emits and the renderer/AG-UI transport already forward — drive a
    ``RemoteQueueView`` to an accurate final state end-to-end (subscribe via
    the public ``subscribe_audit_events`` seam, no private-state peeking).

    Witness ② (#5450) is implicit here: ``next(e for e in captured if
    e.type == "turn_started")`` below raises ``StopIteration`` if the real
    driver never actually dispatched — the test's own core mechanism
    already depends on it, unlike a stub-only check."""
    session = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")

    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=0)

    captured: list = []
    session.subscribe_audit_events(lambda ev: captured.append(ev))

    await session.submit_user_text("alpha")
    # Apply the just-emitted user_submitted delta.
    await settle(session)
    ev = next(e for e in captured if e.type == "user_submitted")
    view.apply_user_submitted(
        msg_id=ev.data["msg_id"], chain_id=ev.data["chain_id"],
        text=ev.data["text"], seq=ev.data["seq"],
    )
    assert [i["text"] for i in view.queue()] == ["alpha"]

    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()

    await settle(session)
    ts = next(e for e in captured if e.type == "turn_started")
    view.apply_turn_started(chain_id=ts.data["chain_id"], seq=ts.data["seq"])
    assert view.queue() == [], "the dispatched item must leave the queue model"

    _llm_stub.release.set()
    await turn_task


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_a_submission_while_the_reply_is_still_streaming_reaches_the_queue_model(
    tmp_path, _llm_stub,
):
    """Tier 2: #3688 — a message submitted WHILE an earlier turn is still
    in-flight (dispatched, not yet finished) must still be accepted by
    ``RemoteQueueView``'s seq gate.

    #3688's own hypothesis (unverified when filed): the already-applied
    ``turn_started`` for the in-flight turn advances ``_last_seq``, so a
    LATER ``user_submitted``'s seq could be <= that value and get silently
    dropped by the ``seq <= self._last_seq`` gate — reproducing the owner's
    "message sent while streaming never appears in the sent queue" report.

    FALSIFY: this test drives the exact interleaving (submit → dispatch →
    turn stays in-flight (hanging, standing in for "still streaming") →
    submit AGAIN) through a real ``Session`` (`_bump_queue_seq()` is a
    plain, synchronous, monotonically-increasing counter — the SAME single
    counter every queue-delta emission call increments, in call order, with
    no `await` between check-and-increment), and asserts the SECOND
    submission's delta is accepted, not dropped. It passes today: the
    seq-gate mechanism itself is not the defect. That does not close #3688
    — the owner's symptom is real — it REDIRECTS it, per the issue's own
    decision tree, to the render side (``apply_user_submitted``'s caller
    returning True but the sent-queue widget not showing the item), which
    this test does not cover.
    """
    session = _make_session(tmp_path / "state.wal", tmp_path / "snapshot.json")

    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=0)

    captured: list = []
    session.subscribe_audit_events(lambda ev: captured.append(ev))

    await session.submit_user_text("alpha")
    await settle(session)
    ev = next(e for e in captured if e.type == "user_submitted")
    view.apply_user_submitted(
        msg_id=ev.data["msg_id"], chain_id=ev.data["chain_id"],
        text=ev.data["text"], seq=ev.data["seq"],
    )

    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()

    await settle(session)
    ts = next(e for e in captured if e.type == "turn_started")
    view.apply_turn_started(chain_id=ts.data["chain_id"], seq=ts.data["seq"])
    assert view.queue() == []  # alpha dispatched; turn now "streaming" (hung)

    # The reply is in flight (streaming) — submit a SECOND message now.
    captured.clear()
    await session.submit_user_text("beta")
    await settle(session)
    ev2 = next(e for e in captured if e.type == "user_submitted")
    applied = view.apply_user_submitted(
        msg_id=ev2.data["msg_id"], chain_id=ev2.data["chain_id"],
        text=ev2.data["text"], seq=ev2.data["seq"],
    )

    assert applied is True, (
        f"#3688 reproduced: mid-stream user_submitted (seq={ev2.data['seq']}, "
        f"last_seq={view._last_seq}) was rejected by the seq gate"
    )
    assert [i["text"] for i in view.queue()] == ["beta"]

    _llm_stub.release.set()
    await turn_task
