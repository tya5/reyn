"""Tier 2: Session.await_quiescent — the global-rewind quiescence primitive.

ADR-0038 Stage 1c part-1. Real `Session` + `StateLog` + real asyncio tasks
(no mocks). `await_quiescent()` must return only once no turn / skill / plan is
in flight, and — the correctness-critical invariant — **no WAL append lands after
it returns** (a straggler past the future rewind reset-record would contaminate
the active branch).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionAnswer, UserIntervention


def _session(tmp_path: Path, log: StateLog, *, agent: str = "alpha") -> Session:
    session = Session(
        agent_name=agent, state_log=log, snapshot_path=tmp_path / "snap.json",
    )
    session.register_intervention_listener("test")
    return session


@pytest.mark.asyncio
async def test_await_quiescent_returns_when_idle(tmp_path):
    """Tier 2: with nothing in flight, await_quiescent returns promptly."""
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)
    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)


@pytest.mark.asyncio
async def test_await_quiescent_joins_inflight_then_no_append(tmp_path):
    """Tier 2: await_quiescent joins an in-flight skill, then no WAL append after.

    The in-flight task does its final WAL append before finishing;
    await_quiescent must wait for that (the join), and once it returns the WAL
    seq must be stable — the no-append-after-quiescent invariant the rewind
    reset-record relies on.
    """
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)

    appended = asyncio.Event()

    async def _inflight():
        # An in-flight skill's terminal WAL append (e.g. recording its end).
        await log.append("skill_discarded", target="alpha", run_id="s1")
        appended.set()

    task = asyncio.create_task(_inflight())
    session.running_skills["s1"] = task

    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)

    assert appended.is_set()              # joined: in-flight append completed first
    assert task.done()
    seq_after = log.current_seq
    await asyncio.sleep(0.02)             # give any straggler a chance to fire
    assert log.current_seq == seq_after   # invariant: no append after quiescent


@pytest.mark.asyncio
async def test_await_quiescent_after_cancel_inflight(tmp_path):
    """Tier 2: cancel_inflight then await_quiescent settles with no append after.

    A cancelled in-flight task is joined; once await_quiescent returns the WAL is
    stable (the cancel + quiesce ordering the global rewind uses).
    """
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)

    started = asyncio.Event()

    async def _inflight():
        started.set()
        await asyncio.sleep(5)            # long-running; will be cancelled

    task = asyncio.create_task(_inflight())
    session.running_skills["s1"] = task
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await session.cancel_inflight()       # cooperative cancel + task.cancel()
    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)

    assert task.done()                    # cancelled task joined
    seq_after = log.current_seq
    await asyncio.sleep(0.02)
    assert log.current_seq == seq_after   # no append after quiescent


# ── per-source-type coverage (ADR-0038 Stage 1c coverage-fix, #1533) ──────────
# One no-append-after-quiescent test per append-capable spawned task type beyond
# the obvious skill/plan set: chain-timeout timers, fire-and-forget intervention
# dispatch, fire-and-forget intervention_answer_consumed. Each must be cancelled +
# joined by await_quiescent so no WAL append can land past the future reset-record.


@pytest.mark.asyncio
async def test_await_quiescent_cancels_chain_timeout_timer_no_append(tmp_path):
    """Tier 2: await_quiescent cancels an armed chain-timeout watchdog (no append).

    A chain-timeout watchdog, on fire, appends ``chain_timeout_fired`` to the WAL.
    await_quiescent must cancel+join it so it cannot fire after a rewind reset.
    The watchdog is armed with a real (short) timeout and a minimal real on_fire
    that performs the WAL append it would do in production; the test proves the
    timer is cancelled before that fire can happen.
    """
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)

    # The chain must be registered so the watchdog's "still pending?" check passes
    # and on_fire would actually run (otherwise the fire short-circuits).
    await session._chains.register(
        chain_id="c1", from_user=True, depth=0, original_text="q", sender=None,
    )

    async def _on_fire(chain_id: str) -> None:
        # Minimal stand-in for the production fire path's terminal WAL append.
        await log.append("chain_timeout_fired", agent="alpha", chain_id=chain_id)

    # Short timeout so the watchdog WOULD fire almost immediately if not cancelled.
    session._chains._chain_timeout_seconds = 0.05
    session._chains.arm_timeout("c1", on_fire=_on_fire)

    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)

    seq_after = log.current_seq
    await asyncio.sleep(0.12)                  # well past the 0.05s timeout
    # Behavioral proof of cancellation: had the watchdog NOT been cancelled it
    # would have fired by now and appended chain_timeout_fired — seq stability
    # across this window shows await_quiescent cancelled it.
    assert log.current_seq == seq_after


@pytest.mark.asyncio
async def test_await_quiescent_cancels_intervention_dispatch_no_append(tmp_path):
    """Tier 2: await_quiescent cancels an in-flight intervention-dispatch task.

    The fire-and-forget dispatch task (claim_pending_intervention →
    ensure_future(_dispatch_intervention)) awaits the user-answer future
    (``iv.future``) indefinitely. await_quiescent must cancel+join it — a bare
    join would hang — so no later WAL append lands after a rewind reset. The
    dispatch path's ``finally`` appends ``intervention_resolved`` on the cancel
    exit, which the join settles before await_quiescent returns.
    """
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    # Seed the stalled queue directly (test precedent: test_pending_intervention_268)
    # so claim re-dispatches through the real path.
    session._interventions._stalled[iv.id] = iv

    view = await session.claim_pending_intervention(iv.id, "new-channel")
    assert view is not None
    await asyncio.sleep(0)                     # let the dispatch task reach its await

    # Must RETURN (not hang) despite the indefinitely-blocking dispatch task.
    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)

    # Public proof the dispatch task was cancelled (not left blocked/untracked):
    # the task awaits iv.future, so cancelling the task cancels iv.future.
    assert iv.future.cancelled()
    seq_after = log.current_seq
    await asyncio.sleep(0.02)
    assert log.current_seq == seq_after        # no append after quiescent


@pytest.mark.asyncio
async def test_await_quiescent_settles_intervention_answer_consumed_no_append(tmp_path):
    """Tier 2: await_quiescent settles the fire-and-forget answer-consumed task.

    consume_buffered_intervention_answer schedules a fire-and-forget
    ``record_intervention_answer_consumed`` WAL append. await_quiescent must
    track + settle it so no such append lands after a rewind reset.
    """
    log = StateLog(tmp_path / "state.wal")
    session = _session(tmp_path, log)

    session._buffered_intervention_answers["run-1"] = InterventionAnswer(text="ok")
    answer = session.consume_buffered_intervention_answer("run-1")
    assert answer is not None and answer.text == "ok"

    await asyncio.wait_for(session.await_quiescent(), timeout=2.0)

    # Behavioral proof of tracking: an *untracked* fire-and-forget consume task
    # would still be pending here and would append intervention_answer_consumed
    # during this sleep (after quiescent returned) — advancing the seq. Seq
    # stability shows await_quiescent tracked + settled it before returning.
    seq_after = log.current_seq
    await asyncio.sleep(0.02)
    assert log.current_seq == seq_after        # no append after quiescent
