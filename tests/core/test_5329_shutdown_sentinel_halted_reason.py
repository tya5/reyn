"""Tier 2: #5329 B — the shutdown-sentinel branch of ``run_one_iteration``
now emits ``session_halted(reason="shutdown_requested")``, matching its two
siblings (``durability_failure``, ``cancelled``) instead of returning
``False`` silently.

Not a new mechanism: the #2280 ``session_halted`` audit-event surface
already exists and is already rendered by both the TUI status line and the
plain-CUI toolbar (see #3377's own test file,
``test_3377_run_loop_survives_turn_cancel.py``, for the ``cancelled``
sibling's own witness of the same surface, and #2280's own
``test_2280_durability_halt_observability.py`` for the ``durability_failure``
sibling's). This closes a gap in applying it — one of the three branches
that can make ``run_one_iteration`` return ``False`` was the only one of the
three left silent. Found while chasing owner's "quota exhaustion makes the
TUI vanish with nothing shown" report (#5329): ``Session.shutdown()``'s own
sentinel path produced no record a caller (or a human reading
``.reyn/events``) could use to tell "the session was asked to stop" apart
from "durability failed" or "was cancelled".
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.durability_worker import DurabilityWorker
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

AGENT = "shutdown-sentinel-agent"


def _make_session(tmp_path: Path, *, worker: "DurabilityWorker | None" = None) -> "tuple[Session, StateLog]":
    state_log = StateLog(tmp_path / "state.wal", worker=worker) if worker else StateLog(tmp_path / "state.wal")
    session = make_session(
        agent_name=AGENT,
        state_log=state_log,
        snapshot_path=tmp_path / "snapshot.json",
    )
    return session, state_log


async def _inject_persistent_durability_failure(log: StateLog) -> None:
    """Genuinely trigger the real §4-exhausted fire-and-forget durable-write
    failure that latches ``StateLog.durability_failed`` — the same real
    trigger ``test_2280_durability_halt_observability.py`` uses, never a
    private-attribute poke (``durability_failed`` is a read-only property
    over ``DurabilityWorker``'s own internal latch)."""
    async def _boom() -> None:
        raise OSError("simulated disk death")

    log.submit_durable_nowait(_boom)
    await log.flush()
    assert log.durability_failed, "setup: the injected failure must latch durability_failed"


@pytest.mark.asyncio
async def test_shutdown_emits_session_halted_shutdown_requested(tmp_path):
    """Tier 2: #5329 B's own witness — calling ``Session.shutdown()`` (the
    sole producer of the shutdown sentinel, ``session.py``'s own
    ``await self.inbox.put(("shutdown", {}))``) must leave a
    ``session_halted(reason="shutdown_requested")`` record, not a silent
    ``False``.

    Strip-falsifier: removing the new ``if self._halted_reason is None: ...
    emit(...)`` block from the ``trigger is None`` branch turns this red —
    ``run_one_iteration`` still returns ``False`` (the loop still stops
    correctly) but no event is emitted, so ``halted`` below comes back
    empty. Verified by hand this session."""
    session, state_log = _make_session(tmp_path)
    collected = collect_events(session._audit_events)
    run_task = asyncio.create_task(session.run())
    try:
        # Let the loop actually reach its idle inbox wait before shutting
        # down — shutting down before the loop has started would not
        # exercise the real drain_to_wake() -> trigger is None path.
        await asyncio.sleep(0)
        assert session.halted_reason is None

        await session.shutdown()
        await asyncio.wait_for(run_task, timeout=5)
        await settle(session._audit_events)

        assert session.halted_reason == "shutdown_requested", (
            f"expected the shutdown sentinel to set halted_reason, got "
            f"{session.halted_reason!r}"
        )
        halted = [e for e in collected if e.type == "session_halted"]
        (halt_event,) = halted  # the #2280 at-most-once guard: exactly one
        assert halt_event.data.get("reason") == "shutdown_requested"
    finally:
        await state_log.aclose()


@pytest.mark.asyncio
async def test_durability_failure_sibling_still_emits_exactly_one(tmp_path):
    """Tier 2: #5329 B non-regression — the ``durability_failure`` sibling
    (the branch immediately above the one this PR touches) must still emit
    its own ``session_halted`` exactly once. This is witness #6 from
    architect's #5329 design: what's fragile about this change is not the
    NEW emit firing, but the EXISTING siblings staying at "one each"."""
    worker = DurabilityWorker(max_write_attempts=1)  # fail-fast, no slow backoff
    session, state_log = _make_session(tmp_path, worker=worker)
    collected = collect_events(session._audit_events)

    await _inject_persistent_durability_failure(state_log)

    result = await session.run_one_iteration()
    await settle(session._audit_events)

    assert result is False
    assert session.halted_reason == "durability_failure"
    halted = [e for e in collected if e.type == "session_halted"]
    (halt_event,) = halted  # the #2280 at-most-once guard: exactly one
    assert halt_event.data.get("reason") == "durability_failure"

    # Calling it again must NOT emit a second one — the at-most-once guard
    # this branch shares with the shutdown-sentinel branch this PR adds.
    await session.run_one_iteration()
    await settle(session._audit_events)
    halted_again = [e for e in collected if e.type == "session_halted"]
    (still_one,) = halted_again  # unpack fails if a second one landed
    assert still_one is halt_event

    await state_log.aclose()


@pytest.mark.asyncio
async def test_shutdown_sentinel_branch_respects_an_already_set_reason(tmp_path):
    """Tier 2: #5329 B non-regression — witness #6's other half. If a
    sibling (``durability_failure`` / ``cancelled``) already set
    ``halted_reason`` before the shutdown-sentinel branch this PR touches
    runs, that branch must NOT overwrite it or emit a second
    ``session_halted``. Real #3377 sibling shape: ``run()``'s own
    ``except asyncio.CancelledError`` sets ``halted_reason`` BEFORE the
    loop condition is even re-checked, so a shutdown sentinel already
    queued at that point must find the guard already closed.

    This exercises the ACTUAL branch (via a real ``run_one_iteration()``
    call reaching ``trigger is None``), not a hand-simulated substitute —
    the sentinel is genuinely queued first via ``Session.shutdown()``, so
    ``drain_to_wake()`` returns it immediately without blocking."""
    session, state_log = _make_session(tmp_path)
    collected = collect_events(session._audit_events)

    # Simulate the sibling having already fired (as run()'s CancelledError
    # handler does, immediately, before its while-loop's next condition
    # check) THEN queue the shutdown sentinel — the shape a real cancel
    # racing a real shutdown produces.
    #
    # #5557 positive control (observed, not assumed): stripped this manual
    # emit and drove session.run() through a REAL Task.cancel() instead —
    # production's own except asyncio.CancelledError handler in
    # Session.run() genuinely emits session_halted(reason="cancelled")
    # through this exact code path with no test-side fabrication. Observed
    # directly: `STRIP-TEST OBSERVED halted_reason: cancelled` /
    # `STRIP-TEST OBSERVED events: [..., 'session_halted', ...]`. The SAME
    # real-cancel path is independently pinned end-to-end by
    # test_3377_run_loop_survives_turn_cancel.py::test_cancelling_the_
    # session_still_stops_the_loop_and_records_it, so this test's own
    # manual emit — standing in for that sibling path so THIS test can
    # focus on its own claim (the shutdown-sentinel branch must not
    # double-emit) — is legitimate driving, not a fake.
    session._halted_reason = "cancelled"
    session._audit_events.emit("session_halted", reason="cancelled")
    await settle(session._audit_events)
    await session.shutdown()

    result = await session.run_one_iteration()
    await settle(session._audit_events)

    assert result is False
    assert session.halted_reason == "cancelled", (
        "the shutdown-sentinel branch must not overwrite an already-set "
        f"halted_reason; got {session.halted_reason!r}"
    )
    halted = [e for e in collected if e.type == "session_halted"]
    # the shutdown-sentinel branch must not have emitted a SECOND one
    # despite halted_reason already being set — unpack fails otherwise
    (halt_event,) = halted
    assert halt_event.data.get("reason") == "cancelled"

    await state_log.aclose()
