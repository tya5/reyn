"""Tier 2: ChatSession.await_quiescent — the global-rewind quiescence primitive.

ADR-0038 Stage 1c part-1. Real `ChatSession` + `StateLog` + real asyncio tasks
(no mocks). `await_quiescent()` must return only once no turn / skill / plan is
in flight, and — the correctness-critical invariant — **no WAL append lands after
it returns** (a straggler past the future rewind reset-record would contaminate
the active branch).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog


def _session(tmp_path: Path, log: StateLog, *, agent: str = "alpha") -> ChatSession:
    session = ChatSession(
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
