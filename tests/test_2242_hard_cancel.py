"""Tier 2: #2242 — hard-cancel for turn interrupt (mid-flight LLM call).

Pre-#2242, ``cancel_inflight()`` only set a COOPERATIVE flag
(``RouterLoopDriver._turn_cancel_requested``), checked at the TOP of each
router-loop iteration — i.e. BEFORE the next LLM call, never during one. A
turn stuck mid-generation could not be interrupted; the spinner sat for the
full duration of the in-flight LLM call (~20s UX gap, see
``tests/test_turn_cancel_1468.py`` for that pre-existing cooperative layer,
unchanged by this PR).

#2242 makes the turn body a per-turn CANCELLABLE SUB-TASK
(``Session._turn_owner_task = asyncio.create_task(self._run_turn_body(...))``,
awaited by ``run_one_iteration``) and has ``cancel_inflight()`` call
``_turn_owner_task.cancel()`` directly — injecting ``CancelledError`` at
whatever await point the sub-task is CURRENTLY suspended on (mid-generation:
the LLM call itself), aborting it immediately instead of waiting for the next
iteration boundary.

WAL-invariants pinned here (ADR-0038 Stage 1c / architect's #2242 design
comment):

  1. A cancelled turn's result is NEVER appended — CancelledError unwinds the
     turn-body task out of the in-flight await, so every statement AFTER that
     await (parsing the response, appending it to history) never executes.
     Proven here by RELEASING the hung LLM call AFTER the cancel: if the
     cancellation were merely cooperative (or simply delayed), the awaited
     call would resume and the reply WOULD land — this test asserts it never
     does.
  2. A fire-and-forget WAL-append task tracked BEFORE the cancelled turn's LLM
     await (``Session._track_wal_task`` — e.g. a buffered-intervention-answer
     consume) is NOT touched by cancelling ``_turn_owner_task`` (a distinct
     task) and is JOINED by ``await_quiescent()`` on the cancel path before
     ``run_one_iteration`` returns — it survives.
  3. The session (driver task) survives a hard-cancel: ``cancel_inflight()``
     swallows only its OWN cancellation (tracked via
     ``_turn_cancel_self_initiated``); ``run_one_iteration`` returns normally
     and a SUBSEQUENT turn runs to completion — the agent is not torn down.

Real ``Session`` / ``StateLog`` / ``AgentSnapshot`` (no mocks) — only the LLM
boundary is replaced with a plain, controllable async function assigned onto
``session._loop_driver.run_turn``, exactly the seam
``tests/test_2884_hook_driven_turns_truncation_falsify.py`` uses to isolate
the mechanism under test from RouterLoop's own internals (already covered by
``tests/test_turn_cancel_1468.py``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionAnswer

AGENT = "hard-cancel-agent"
_LANDED_REPLY = "SHOULD-NOT-LAND-IF-HARD-CANCELLED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = Session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _install_hanging_run_turn(session: Session) -> tuple[asyncio.Event, asyncio.Event]:
    """Replace RouterLoopDriver.run_turn with a controllable hang — a real,
    plain async function method-assigned onto the instance (the SAME
    no-mock seam ``test_2884_...``'s ``_fake_run_turn`` uses), not a
    MagicMock/AsyncMock. ``call_started`` fires the instant the "LLM call"
    begins (simulating the moment RouterLoop would be suspended inside its
    ``litellm.acompletion`` await); ``release`` is set by the TEST, after the
    cancel, to prove the resumed coroutine's post-await code never runs on a
    truly hard-cancelled task."""
    call_started = asyncio.Event()
    release = asyncio.Event()

    async def _hanging_run_turn(user_text: str, chain_id: str) -> None:
        call_started.set()
        await release.wait()  # simulates an in-flight LLM call, suspended
        # Only reached if the awaiting task was NOT actually cancelled —
        # mirrors what a real completed run_turn does (append the reply).
        session._append_history(ChatMessage(role="assistant", content=_LANDED_REPLY, ts=_now()))

    session._loop_driver.run_turn = _hanging_run_turn  # type: ignore[method-assign]
    return call_started, release


async def _seed_prior_fire_and_forget_wal_task(session: Session, run_id: str) -> None:
    """Seed + consume a buffered intervention answer — the production
    fire-and-forget WAL-append seam ``Session.consume_buffered_intervention_answer``
    drives via ``_track_wal_task`` (see that method's #2242 docstring note and
    ``consume_buffered_intervention_answer``'s R-D12 comment). This is the
    concrete stand-in for WAL-invariant 2's "a fire-and-forget append task
    spawned before the cancelled turn's LLM await" — tracked BEFORE the hung
    turn is even started here, mirroring a real prior-turn append still
    settling when the NEXT turn gets hard-cancelled."""
    session.buffered_intervention_answers[run_id] = InterventionAnswer(text="prior answer")
    answer = session.consume_buffered_intervention_answer(run_id)
    assert answer is not None and answer.text == "prior answer"  # sanity: seeded + popped


@pytest.mark.asyncio
async def test_hard_cancel_mid_generation_no_result_append_and_agent_survives(tmp_path):
    """Tier 2: #2242 cancel-falsify. Cancelling DURING a hung "LLM call"
    (a) never lands the reply (invariant 1), (b) leaves the active branch
    clean — no partial/cancelled turn markers accumulate in history beyond
    the pre-cancel user message, (c) the agent survives — a subsequent
    ordinary turn completes normally, and (d) a fire-and-forget WAL-append
    task tracked before the hang settles via await_quiescent (invariant 2).

    STRIP-RED: reverting ``Session.run_one_iteration``'s per-turn sub-task
    (back to running the dispatch inline on the driver task, as before #2242)
    makes ``cancel_inflight()``'s ``_turn_owner_task.cancel()`` cancel the
    OUTER (run_one_iteration) task itself instead of an isolated sub-task —
    the test's ``asyncio.wait_for(task, ...)`` then raises CancelledError
    instead of completing, and the fire-and-forget join never runs (RED).
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)

    prior_run_id = "prior-answer-run"
    await _seed_prior_fire_and_forget_wal_task(session, prior_run_id)

    call_started, release = _install_hanging_run_turn(session)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-hard-cancel"})
    turn_task = asyncio.create_task(session.run_one_iteration())

    await asyncio.wait_for(call_started.wait(), timeout=5)
    # The "LLM call" is now in flight (suspended on `release.wait()`).
    result = await session.cancel_inflight()
    assert "cancel" in result.lower()

    # Release the hang AFTER the cancel: if the sub-task were only
    # cooperatively (or not truly) cancelled, it would resume here and the
    # reply WOULD land — proving the difference between hard-cancel and a
    # merely-delayed completion.
    release.set()

    # (c) agent survives: run_one_iteration returns normally (True), not an
    # exception — cancel_inflight() swallowed its own CancelledError.
    completed = await asyncio.wait_for(turn_task, timeout=5)
    assert completed is True

    # (a) the cancelled turn's result never landed.
    assert not any(m.content == _LANDED_REPLY for m in session.history), (
        "a hard-cancelled turn's LLM reply must never be appended, even after "
        "the underlying hung call is released post-cancel"
    )
    # (b) branch clean: only the pre-cancel user message is present (no
    # partial assistant/tool entries from the aborted turn).
    roles = [m.role for m in session.history]
    assert roles == ["user"], f"expected only the user message to survive; got {roles}"

    # (d) the prior fire-and-forget WAL-append task survived (joined by
    # await_quiescent on the cancel path) — its durable effect is visible in
    # the WAL, not lost/orphaned by the sub-task cancellation.
    await session.journal.flush()
    wal_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in wal_lines
    ), "the prior fire-and-forget intervention_answer_consumed append must survive the hard-cancel"

    # (c) continued: a SUBSEQUENT ordinary turn completes normally — the
    # session/driver was not torn down by the hard-cancel.
    async def _normal_run_turn(user_text: str, chain_id: str) -> None:
        session._append_history(ChatMessage(role="assistant", content="normal reply", ts=_now()))

    session._loop_driver.run_turn = _normal_run_turn  # type: ignore[method-assign]
    await session._put_inbox("user", {"text": "again", "chain_id": "c-after-cancel"})
    next_completed = await asyncio.wait_for(session.run_one_iteration(), timeout=5)
    assert next_completed is True
    assert any(m.content == "normal reply" for m in session.history), (
        "a normal turn after a hard-cancel must complete and append its reply — "
        "the agent must survive to serve the next turn"
    )

    await state_log.aclose()


@pytest.mark.asyncio
async def test_hard_cancel_prior_append_survives_wal_truncation(tmp_path):
    """Tier 2: #2242 truncate-falsify (CLAUDE.md recovery-feature PR gate).

    Repeats the hard-cancel scenario, then pushes filler WAL events past the
    surviving fire-and-forget append's source events and truncates below
    them (mirroring ``test_2884_hook_driven_turns_truncation_falsify.py``).
    Reconstructing (fresh Session + StateLog: load snapshot, replay the WAL
    tail) must still show the buffered-answer-consumed state as durable —
    proving the hard-cancel path does not leave the fire-and-forget append in
    a state that a subsequent truncation+reconstruction cycle would corrupt
    or lose. RED if the snapshot-side bookkeeping (``buffered_intervention_
    answers`` popped on consume, backed by ``AgentSnapshot``) were skipped or
    raced by the cancel path: reconstruction would still show the answer as
    OUTSTANDING (not consumed) or missing the consumed marker in the WAL.
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)

    prior_run_id = "prior-answer-run-truncate"
    await _seed_prior_fire_and_forget_wal_task(session, prior_run_id)

    call_started, release = _install_hanging_run_turn(session)
    await session._put_inbox("user", {"text": "hello", "chain_id": "c-truncate"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    await asyncio.wait_for(call_started.wait(), timeout=5)
    await session.cancel_inflight()
    release.set()
    await asyncio.wait_for(turn_task, timeout=5)
    await session.journal.flush()

    # sanity: the consumed marker's source event is durable pre-truncation.
    pre_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in pre_truncate_lines
    ), "sanity: the consumed-answer source event must be durable pre-truncation"
    assert prior_run_id not in session.buffered_intervention_answers, (
        "sanity: the answer must already be popped from the live buffer"
    )

    # push filler events far past the source events, then truncate below them.
    for i in range(150):
        await state_log.append("inbox_put", n=i)
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 2, (
        f"the buffered/consumed source events must be truncated below the floor; "
        f"dropped={stats['dropped']}"
    )
    post_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert not any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in post_truncate_lines
    ), "the consumed-answer source event must actually be gone post-truncation"

    await state_log.aclose()  # simulate the crash: tear down run1's WAL worker

    # reconstruct (simulates a restart): a FRESH StateLog + Session over the
    # SAME wal/snapshot (mirrors AgentRegistry.restore_all).
    session2, state_log2 = _make_session(wal, snapshot_path)
    snap = AgentSnapshot.load(AGENT, snapshot_path)
    events = list(state_log2.iter_from(snap.applied_seq))
    snap.apply_events(events)
    session2.restore_state(snap)

    assert prior_run_id not in session2.buffered_intervention_answers, (
        "the answer must stay CONSUMED after reconstruction — the hard-cancel "
        "path must not leave it re-appearing as outstanding post-truncation"
    )

    await state_log2.aclose()
