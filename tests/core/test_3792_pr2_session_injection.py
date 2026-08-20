"""Tier 2: #3792 PR2 — the Session-level peek/commit primitives for mid-turn
``CLIENT_INPUT`` injection.

Covers, per architect's issue #3792 Test plan (the Session-level items; the
RouterLoop-level wire-position / strip-falsify witnesses live in
``tests/llm/test_3792_pr2_router_loop_injection.py``):

- **origin gate** — only ``TurnOrigin.CLIENT_INPUT`` is peek-eligible; all 9
  members enumerated (vacuity guard: fails loud if the enumeration is empty).
- **order** — an ineligible head STOPS peek (never skips ahead); the SAME
  item then surfaces, in order, via the ordinary turn-boundary
  ``_consume_inbox`` drain.
- **carry-forward** — a peeked-but-never-committed item (the abnormal-exit
  case: cancel / cap / overflow / LLM exception happened before commit) is
  not lost — the next ``_consume_inbox`` call returns the SAME item.
- **the atomic commit unit** — history append + snapshot/WAL prune +
  ``turn_started`` promote-delta, all three.
- **truncate-falsify** (CLAUDE.md recovery-feature PR gate) — a committed
  injection's inbox entry does not resurrect after a WAL truncation below
  its source events.
- **loop valve** — commit does not reset ``_hook_driven_turns``.

Real ``Session``/``StateLog``/``SnapshotJournal`` throughout (the
``tests/interfaces/test_3300_p3_cancel_by_id.py`` convention) — no ``unittest.mock``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session
from tests._support.events import settle

AGENT = "pr2-injection-agent"


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _reconstruct(agent_name: str, snapshot_path: Path, state_log: StateLog) -> AgentSnapshot:
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    events = list(state_log.iter_from(snap.applied_seq))
    snap.apply_events(events)
    return snap


# ---------------------------------------------------------------------------
# Origin gate — exhaustive, vacuity-guarded
# ---------------------------------------------------------------------------


_NON_CLIENT_INPUT_KWARGS: dict[TurnOrigin, dict] = {
    TurnOrigin.AGENT_REQUEST: {"from_agent": "a", "request": "r", "depth": 1, "chain_id": "c1"},
    TurnOrigin.AGENT_RESPONSE: {"from_agent": "a", "response": "r", "depth": 1, "chain_id": "c1"},
    TurnOrigin.PIPELINE_RESULT: {"run_id": "run1", "pipeline_name": "p", "status": "ok", "text": "t"},
    TurnOrigin.AGENT_STEP: {"seq": 1},
    TurnOrigin.EXTERNAL_MESSAGE: {"text": "hi", "source": "slack"},
    TurnOrigin.CRON: {"job": "nightly"},
    TurnOrigin.HOOK: {"name": "on_idle"},
    TurnOrigin.PIPELINE_NUDGE: {"run_id": "run1"},
    # Proposal 0067 P5 (#3978): send_to_session / run_prompt(attached), a
    # peer session's text — see TurnOrigin.PEER_SESSION's own docstring.
    TurnOrigin.PEER_SESSION: {"text": "hi", "from_session": "a2a:peer"},
}


@pytest.mark.asyncio
async def test_only_client_input_origin_is_peek_eligible(tmp_path):
    """Tier 2: #3792 — exhaustive origin gate over ALL 10 ``TurnOrigin``
    members (vacuity guard: asserts the enumeration itself is non-empty and
    matches the expected 10, so a future member silently added to the enum
    without a corresponding case here is caught, not silently vacuous).

    Falsification (performed during review): removing the
    ``if kind != TurnOrigin.CLIENT_INPUT: return None`` check from
    ``Session._peek_mid_turn_injection`` makes this test go RED — EVERY
    origin (not just CLIENT_INPUT) becomes peek-eligible.
    """
    all_origins = list(TurnOrigin)
    # Vacuity guard: if TurnOrigin grew, shrank, or the enumeration came back
    # empty, this set-equality against the explicit expected membership
    # (below) fails LOUD — no separate count needed, a set-size mismatch is
    # already a set-inequality.
    assert set(_NON_CLIENT_INPUT_KWARGS) | {TurnOrigin.CLIENT_INPUT} == set(all_origins), (
        "TurnOrigin's membership changed — update _NON_CLIENT_INPUT_KWARGS "
        "above to cover the new/removed member before trusting this gate again"
    )

    for origin, kwargs in _NON_CLIENT_INPUT_KWARGS.items():
        session, state_log = _make_session(
            tmp_path / f"{origin.value}.wal", tmp_path / f"{origin.value}.json",
        )
        await session._put_inbox(origin, kwargs)
        result = await session._inbox_arbiter.peek_mid_turn_injection()
        assert result is None, f"origin {origin!r} must NOT be peek-eligible"
        await state_log.aclose()

    # Positive control: CLIENT_INPUT (via the real submit_user_text path) IS eligible.
    session, state_log = _make_session(
        tmp_path / "client_input.wal", tmp_path / "client_input.json",
    )
    await session.submit_user_text("hello")
    result = await session._inbox_arbiter.peek_mid_turn_injection()
    assert result is not None
    assert result["payload"]["text"] == "hello"
    await state_log.aclose()


# ---------------------------------------------------------------------------
# Order — ineligible head stops, never skips ahead
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ineligible_head_blocks_peek_and_surfaces_via_normal_drain_in_order(tmp_path):
    """Tier 2: #3792 — an ineligible-origin head STOPS peek (returns None)
    rather than skipping ahead to a later eligible item; the SAME ineligible
    item is what the ordinary turn-boundary ``_consume_inbox`` returns next
    — arrival order preserved, nothing silently reordered.

    Falsification (performed during review): making peek skip past an
    ineligible head to find the next eligible item makes this test go RED
    — the origin returned by the first ``_consume_inbox`` after the peek
    attempt would be ``CLIENT_INPUT`` (the 2nd item), not ``AGENT_REQUEST``
    (the 1st, ineligible, item) — the assertion on ``kind`` below would fail.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "a", "request": "r", "depth": 1, "chain_id": "c1"},
    )
    await session.submit_user_text("second, eligible")

    peeked = await session._inbox_arbiter.peek_mid_turn_injection()
    assert peeked is None, "the ineligible AGENT_REQUEST head must block the peek"

    kind, payload = await session._inbox_arbiter.consume_inbox()
    assert kind == TurnOrigin.AGENT_REQUEST, (
        "the FIRST item _consume_inbox returns must be the same ineligible "
        "head the peek saw — not the 2nd (eligible) item out of order"
    )
    assert payload["request"] == "r"

    kind2, payload2 = await session._inbox_arbiter.consume_inbox()
    assert kind2 == TurnOrigin.CLIENT_INPUT
    assert payload2["text"] == "second, eligible"

    await state_log.aclose()


# ---------------------------------------------------------------------------
# Carry-forward — peeked-but-never-committed is not lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_without_commit_carries_forward_to_normal_consume(tmp_path):
    """Tier 2: #3792 — simulates the abnormal-exit case (cancel / cap /
    overflow / LLM exception happened between peek and commit): the peeked
    item is NEVER lost — the next ``_consume_inbox`` (the normal
    turn-boundary drain) returns the exact same item, processed as an
    ordinary new turn, exactly as if the peek had never happened.

    Falsification (performed during review): if ``_consume_inbox`` read
    ``self.inbox.get()`` unconditionally (ignoring
    ``self._inbox_arbiter.pending_inbox_item``), this test goes RED with an indefinite
    hang (the queue is empty — the item is stuck in the buffer forever) —
    demonstrated by using ``asyncio.wait_for`` with a short timeout instead
    of a bare await, so the failure mode is a clean test failure, not a
    stuck CI job.
    """
    import asyncio

    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    await session.submit_user_text("carry me")

    peeked = await session._inbox_arbiter.peek_mid_turn_injection()
    assert peeked is not None
    # Deliberately do NOT commit — simulates the abnormal exit.

    kind, payload = await asyncio.wait_for(session._inbox_arbiter.consume_inbox(), timeout=2.0)
    assert kind == TurnOrigin.CLIENT_INPUT
    assert payload["text"] == "carry me"
    assert payload["_msg_id"] == peeked["msg_id"]

    await state_log.aclose()


# ---------------------------------------------------------------------------
# The atomic commit unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_appends_history_prunes_snapshot_emits_turn_started(tmp_path):
    """Tier 2: #3792 — a successful commit does all three: (1) appends a
    ``role="user"`` entry to ``session.history`` (the restore SSoT), (2)
    prunes the item from ``journal.snapshot.inbox`` (the inbox SSoT), (3)
    emits a ``turn_started`` audit-event carrying the injected message's own
    chain_id — the SAME shape the ordinary turn-boundary promote uses.

    Falsification (performed during review): commenting out any ONE of the
    three statements in ``Session._commit_mid_turn_injection`` makes the
    corresponding assertion below go RED independently of the other two —
    confirming each is actually load-bearing, not incidentally satisfied by
    one of the others.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    msg_id = await session.submit_user_text("inject me", attribution=None)
    peeked = await session._inbox_arbiter.peek_mid_turn_injection()
    assert peeked is not None
    assert peeked["msg_id"] == msg_id

    before_history_len = len(session.history)
    assert any(m["id"] == msg_id for m in session.journal.snapshot.inbox)

    await session._commit_mid_turn_injection(msg_id)
    await settle(session._audit_events)

    # (1) history
    assert len(session.history) == before_history_len + 1
    injected_entry = session.history[-1]
    assert injected_entry.role == "user"
    assert injected_entry.content == "inject me"

    # (2) snapshot prune
    assert not any(m["id"] == msg_id for m in session.journal.snapshot.inbox)

    # (3) turn_started promote
    turn_started_events = [e for e in events if e.type == "turn_started"]
    (only,) = turn_started_events
    assert only.data["kind"] == TurnOrigin.CLIENT_INPUT
    assert only.data["chain_id"] == peeked["payload"]["chain_id"]

    await state_log.aclose()


@pytest.mark.asyncio
async def test_commit_does_not_reset_hook_driven_turns(tmp_path):
    """Tier 2: #3792 — architect's point 4 (the loop valve): a mid-turn
    injection rides inside the ALREADY-running turn's budget, so committing
    one must NOT reset ``_hook_driven_turns`` (unlike an ordinary
    ``CLIENT_INPUT`` turn dispatched via ``run_one_iteration``, which DOES
    reset it — that is a different code path, deliberately not this one).

    Falsification (performed during review): adding
    ``self._hook_driven_turns = 0`` to ``_commit_mid_turn_injection`` (as if
    it were an ordinary new turn) makes this test go RED.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    session._hook_driven_turns = 7  # setup (write): no public setter exists

    msg_id = await session.submit_user_text("inject me")
    await session._inbox_arbiter.peek_mid_turn_injection()
    await session._commit_mid_turn_injection(msg_id)

    assert session.hook_driven_turns == 7

    await state_log.aclose()


# ---------------------------------------------------------------------------
# Truncate-falsify (CLAUDE.md recovery-feature PR gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_truncate_falsify(tmp_path):
    """Tier 2: #3792 — CLAUDE.md recovery-feature PR gate. A committed
    injection's inbox entry (WAL ``inbox_consume`` tombstone + synchronous
    snapshot prune, ``SnapshotJournal.consume_inbox`` — the SAME mechanism
    ``cancel_queued`` uses, per ``tests/interfaces/test_3300_p3_cancel_by_id.py``'s
    identical shape) must NOT resurrect after a WAL truncation below its
    ``inbox_put``/``inbox_consume`` source events.

    Falsification: this reuses the exact mechanism
    ``test_3300_p3_cancel_by_id.py::test_cancel_snapshot_prune_survives_truncate``
    already falsify-verified for ``cancel_inbox`` — ``consume_inbox`` is the
    same shape (synchronous snapshot prune + WAL tombstone, mirrored per its
    own docstring), so this test is the #3792-specific instance of that
    already-proven mechanism, not a novel one.
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)

    msg_id = await session.submit_user_text("inject me")
    keep_id = await session.submit_user_text("stays queued")
    await session._inbox_arbiter.peek_mid_turn_injection()
    await session._commit_mid_turn_injection(msg_id)
    await session.journal.flush()

    assert not any(m["id"] == msg_id for m in session.journal.snapshot.inbox)
    assert any(m["id"] == keep_id for m in session.journal.snapshot.inbox)

    pre_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert any(
        '"inbox_consume"' in ln and f'"msg_id": "{msg_id}"' in ln for ln in pre_truncate_lines
    ), "sanity: the inbox_consume source event must be durable pre-truncation"

    for i in range(150):
        await state_log.append(
            "inbox_put", n=i, target="filler-agent", msg_id=f"filler-{i}",
            msg_kind="user", payload={},
        )
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 3, (
        f"the 3 real source events (2x inbox_put + 1x inbox_consume) must be "
        f"truncated below the floor; dropped={stats['dropped']}"
    )
    post_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert not any('"inbox_consume"' in ln for ln in post_truncate_lines), (
        "the inbox_consume source event must actually be gone from the WAL "
        "post-truncation (not just counted)"
    )

    await state_log.aclose()

    state_log2 = StateLog(wal)
    reconstructed = _reconstruct(AGENT, snapshot_path, state_log2)

    reconstructed_ids = {m["id"] for m in reconstructed.inbox}
    assert msg_id not in reconstructed_ids, (
        "the committed injection must NOT resurrect after WAL truncation "
        "below its own source events"
    )
    assert keep_id in reconstructed_ids, (
        "an uncommitted queued message must SURVIVE the same truncation — "
        "proving this is injection-commit correctness, not blanket data loss"
    )

    await state_log2.aclose()
