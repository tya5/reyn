"""Tier 2: #3377 — Session.run() must not die silently on an escaping cancel.

``CancelledError`` is a ``BaseException``, so no ``except Exception`` on the
path catches it. Before this, a cancel delivered to the per-turn sub-task by
anyone who did not go through ``cancel_inflight()`` was re-raised out of
``run_one_iteration`` and ended ``run()``'s while-loop. The resulting failure
has a distinctive shape and no diagnostic: the inbox keeps being **put** to and
is never **consumed** again, nothing is waiting because no one is left to wait,
and no error is logged.

#3369 closed ONE source (``checkout`` cancelling its own turn). This file pins
the **property** instead, because other routes to that cancel exist (Ctrl-C,
timeouts, other stop-world operations, whatever is added next):

- a cancel aimed at ONE TURN must not kill the loop, and must leave a record;
- a cancel aimed at THE SESSION (a real shutdown) must still stop the loop,
  and must also leave a record.

The witness is deliberately taken at an INTERMEDIATE cross-section — a second
message is put and must be *consumed* — because a terminal-state assertion
cannot tell a surviving loop from a dead one that happened to finish its work.
The cancel is delivered through the real mechanism (``Task.cancel()``, the same
call ``cancel_inflight`` makes), not by raising ``CancelledError`` inside the
test, which would prove nothing about a path that never catches it.

Real ``Session`` / ``StateLog`` (no mocks). #5450 migration: the LLM
boundary is ``@pytest.mark.llm_stub(control="gated")`` — the real
``RouterLoopDriver``/``RouterLoop`` dispatch for real, hung at the real
``litellm.acompletion`` boundary. The stub's ``release`` ``asyncio.Event``
stays set once set() (no auto-reset), which is exactly "first turn hangs,
every later turn returns at once" — no per-turn re-arming needed. "Every
chain_id the run-loop actually dispatched" (the consumed-side witness) is
now the public ``turn_started`` audit-event stream (#5450 witness ②),
replacing the old private closure's own ``seen`` list — the SAME public
seam this file's own dispatch proof now doubles as "the real driver ran".
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import settle

AGENT = "cancel-survival-agent"


def _make_session(tmp_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(tmp_path / "state.wal")
    session = make_session(
        agent_name=AGENT,
        state_log=state_log,
        snapshot_path=tmp_path / "snapshot.json",
    )
    return session, state_log


def _collect(session: Session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran (#5450)."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


def _seen_chain_ids(events: list) -> "set[str]":
    return {e.data.get("chain_id") for e in events if e.type == "turn_started"}


async def _wait_for(predicate, *, delay: float = 0.02) -> None:
    """Unbounded per the owner's testing policy
    (docs/deep-dives/contributing/testing.md, ## Time): no test carries a time
    budget, marker or in-body -- a slower environment only makes this slower,
    never fail it; CI's --timeout=120 is the blast-radius kill-switch, not a
    contract.
    """
    while not predicate():
        await asyncio.sleep(delay)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "reyn.runtime.session"
    ]


async def _stop(session: Session, run_task: asyncio.Task) -> None:
    await session.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_turn_scoped_cancel_leaves_the_run_loop_consuming(tmp_path, _llm_stub):
    """Tier 2: #3377 — a cancel delivered to the per-turn sub-task by
    something other than ``cancel_inflight()`` abandons that turn but must
    NOT end the run-loop.

    The witness is the signature the bug produces: a message is PUT and then
    never CONSUMED. Here the second message must be consumed (its turn
    actually dispatched, the inbox actually drained) — asserting only that
    the run task is still alive would not distinguish a loop that is
    consuming from one parked forever on a dead path.
    """
    session, state_log = _make_session(tmp_path)
    events = _collect(session)
    run_task = asyncio.create_task(session.run())
    try:
        await session._put_inbox("user", {"text": "one", "chain_id": "c-first"})
        await _llm_stub.call_started.wait()

        # The real delivery mechanism, NOT via cancel_inflight(): this is the
        # shape a Ctrl-C / timeout / stop-world operation produces.
        session._turn_owner_task.cancel()

        # THE WITNESS: put, and require that it is consumed.
        await session._put_inbox("user", {"text": "two", "chain_id": "c-second"})
        await _wait_for(lambda: "c-second" in _seen_chain_ids(events))
        assert "c-second" in _seen_chain_ids(events), (
            "the second message was PUT but never CONSUMED — the run-loop died "
            "on a cancel aimed at a single turn"
        )
        assert session.inbox.empty(), "the inbox was not drained"
        assert not run_task.done(), "run() ended on a turn-scoped cancel"
    finally:
        _llm_stub.release.set()
        await _stop(session, run_task)
        await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_turn_scoped_cancel_of_unknown_origin_is_recorded(tmp_path, caplog, _llm_stub):
    """Tier 2: #3377 — surviving is not enough; a turn killed by a cancel we
    did not initiate must leave a record naming the turn, so this can never
    again present as an unexplained stall with nothing in the log.
    """
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")
    session, state_log = _make_session(tmp_path)
    run_task = asyncio.create_task(session.run())
    try:
        await session._put_inbox("user", {"text": "one", "chain_id": "c-orphan"})
        await _llm_stub.call_started.wait()
        session._turn_owner_task.cancel()
        await _wait_for(lambda: any("c-orphan" in m for m in _warnings(caplog)))

        (warning,) = [m for m in _warnings(caplog) if "c-orphan" in m]
        assert "cancel_inflight" in warning, warning
        assert "run-loop survives" in warning, warning
    finally:
        _llm_stub.release.set()
        await _stop(session, run_task)
        await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_self_initiated_cancel_stays_quiet(tmp_path, caplog, _llm_stub):
    """Tier 2: #3377 non-vacuity — ``cancel_inflight()`` is the EXPECTED way
    to abandon a turn (the user pressed cancel), so it must keep surviving
    silently. A warning that fires on every cancel would say nothing about
    whether the cancel had a known origin.
    """
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")
    session, state_log = _make_session(tmp_path)
    events = _collect(session)
    run_task = asyncio.create_task(session.run())
    try:
        await session._put_inbox("user", {"text": "one", "chain_id": "c-user-cancel"})
        await _llm_stub.call_started.wait()

        await session.cancel_inflight()

        await session._put_inbox("user", {"text": "two", "chain_id": "c-after"})
        await _wait_for(lambda: "c-after" in _seen_chain_ids(events))
        assert "c-after" in _seen_chain_ids(events), "the loop must survive its own cancel too"
        assert [m for m in _warnings(caplog) if "c-user-cancel" in m] == [], (
            "a user-initiated turn cancel must not be reported as unexplained"
        )
    finally:
        _llm_stub.release.set()
        await _stop(session, run_task)
        await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_cancelling_the_session_still_stops_the_loop_and_records_it(
    tmp_path, caplog, _llm_stub,
):
    """Tier 2: #3377 — the other half of the design constraint. A cancel
    genuinely directed at the session (a real shutdown) must still end the
    loop; making turn-scoped cancels survivable must not make the session
    un-stoppable.

    And that ending must be legible: ``chat_stopped`` / ``session_completed``
    fire identically on a clean exit, so on their own they cannot say the
    loop was cancelled. ``session_halted`` (the #2280 surface both the TUI
    status line and the plain-CUI toolbar already render) carries that.
    """
    caplog.set_level(logging.WARNING, logger="reyn.runtime.session")
    session, state_log = _make_session(tmp_path)
    events = _collect(session)
    run_task = asyncio.create_task(session.run())
    try:
        # Drive one full turn first, so the cancel below is delivered to a
        # loop that is demonstrably INSIDE its while — cancelling during
        # run()'s start-up would exercise a different path. release() is set
        # BEFORE any push, so this warmup turn never hangs at all — the
        # stub's own gate passes straight through, same as an un-hung
        # control="gated" call always would.
        _llm_stub.release.set()
        await session._put_inbox("user", {"text": "one", "chain_id": "c-warmup"})
        await _wait_for(lambda: "c-warmup" in _seen_chain_ids(events) and session.inbox.empty())
        assert "c-warmup" in _seen_chain_ids(events), "the loop never reached its idle inbox wait"
        assert session.halted_reason is None

        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await settle(session._audit_events)

        assert run_task.done(), "a cancel aimed at the session must stop the loop"
        assert session.halted_reason == "cancelled", (
            f"the loop ended without a halt record; got {session.halted_reason!r}"
        )
        halted = [e for e in events if e.type == "session_halted"]
        (halt_event,) = halted
        assert halt_event.data.get("reason") == "cancelled"
        assert any("cancelled" in m for m in _warnings(caplog))
    finally:
        await state_log.aclose()
