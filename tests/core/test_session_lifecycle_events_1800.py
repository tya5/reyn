"""Tier 2: #1800 slice 5a — session + turn lifecycle audit events.

Four tests verifying the new P6 events fire at the right points:

1. Tier 1 — schema: all four new events are declared in
   EVENT_AUDIT_REQUIREMENTS with the correct field sets.

2. Tier 2 — session_started fires at the start of Session.run(), before
   the first iteration, and session_completed fires in the finally block.
   Observed via the real EventLog subscriber (public API).

3. Tier 2 — turn_started fires once per turn in run_one_iteration(), after
   the trigger is consumed from the inbox, carrying the inbox kind.

4. Tier 2 — turn_completed fires once per turn in _run_router_loop(),
   immediately after RouterLoopDriver.run_turn() returns, carrying chain_id.

Policy compliance (docs/deep-dives/contributing/testing.md):
- No MagicMock / AsyncMock / patch for Session collaborators.
- Real Session, real EventLog, real StateLog.
- Both tests 3 and 4 drive the REAL `RouterLoopDriver.run_turn` /
  RouterLoop chain via `@pytest.mark.llm_stub` (#5103, architect design
  "C2") — only `litellm.acompletion` itself is stubbed, never `run_turn`.
  Test 4 (turn_completed) used to replace `_loop_driver.run_turn`
  directly with a plain async callable purely to get a "how many events
  existed when run_turn was called" observation point (#5103 triage's
  own "timing observation" class). It now reads the SAME before/after
  bracket off the public `stall_trace_armed`/`stall_trace_disarmed`
  audit-events (#5103 ④, this issue's own new seam) — REYN_STALL_TRACE
  is set purely to arm this observation pair; the N-second stall
  detection itself is untested by design (see stall_trace.py).
- Events observed via add_subscriber (public EventLog API), not via
  private state assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test-agent") -> Session:
    """Build a minimal Session wired to tmp_path."""
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _collect_events(session: Session) -> list[dict]:
    """Subscribe a collector to the session's EventLog and return the list.

    The returned list is mutated in-place as events arrive. #4961 C:
    dispatch moved off of ``emit()``'s own synchronous caller onto a
    queue-consumer task — a caller must ``await session._audit_events.drain()``
    (or otherwise yield enough for the consumer to catch up) before this
    list can be trusted to reflect everything emitted so far.
    """
    collected: list[dict] = []

    def _subscriber(event) -> None:
        collected.append({"type": event.type, **event.data})

    # add_subscriber is the public API on EventLog; _audit_events is the
    # session's internal EventLog that all session-level emits target.
    session._audit_events.add_subscriber(_subscriber)
    return collected


def _events_of_type(collected: list[dict], kind: str) -> list[dict]:
    return [e for e in collected if e["type"] == kind]


# ---------------------------------------------------------------------------
# Test 1: Tier 1 — schema declarations
# ---------------------------------------------------------------------------


def test_new_lifecycle_events_declared_in_event_schema() -> None:
    """Tier 1: four new #1800 slice 5a events are declared in
    EVENT_AUDIT_REQUIREMENTS with the correct required field sets.

    FP-0021 audit-completeness invariant: any event kind emitted by
    production code that is missing from EVENT_AUDIT_REQUIREMENTS fails
    the CI invariant test (test_event_audit_invariants.py). Verifying the
    schema here as a fast Tier 1 sanity check, independent of whether the
    emit logic fires.
    """
    for kind, expected_fields in [
        ("session_started", frozenset({"agent_name"})),
        ("session_completed", frozenset({"agent_name"})),
        ("turn_started", frozenset({"kind"})),
        ("turn_completed", frozenset({"chain_id"})),
    ]:
        assert kind in EVENT_AUDIT_REQUIREMENTS, (
            f"#1800 slice 5a: '{kind}' not declared in EVENT_AUDIT_REQUIREMENTS"
        )
        actual = EVENT_AUDIT_REQUIREMENTS[kind]
        assert actual == expected_fields, (
            f"'{kind}' required fields mismatch: expected {expected_fields!r}, "
            f"got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: Tier 2 — session_started fires before first iteration;
#          session_completed fires in the finally block of run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_started_and_completed_emit(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: session_started emits at run() entry before any iteration;
    session_completed emits in the finally block after the loop exits.

    Approach: pre-load a "shutdown" sentinel into the inbox before calling
    run() so run_one_iteration() returns False immediately (trigger is None)
    and run() exits after a single pump. The real Session.run() body —
    including both emit calls — is exercised end-to-end.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    collected = _collect_events(session)

    # Pre-load shutdown sentinel; run_one_iteration()'s _drain_to_wake
    # will read it and return (None, None) → iteration returns False →
    # run() exits the while loop and falls into finally.
    session.inbox.put_nowait(("shutdown", {}))
    await session.run()
    # #4961 C: `session_completed` is emitted right at the tail of
    # run()'s own finally block, with no further internal await after
    # it — `await session.run()` returning does not by itself guarantee
    # the consumer has drained it yet. `drain()` is deterministic
    # (unlike a bare yield, which only helps if exactly one event is
    # pending); Session.run() itself now awaits this at its own real
    # shutdown path too — see its own comment there.
    await session._audit_events.drain()

    started = _events_of_type(collected, "session_started")
    completed = _events_of_type(collected, "session_completed")

    # Unpack-enforcement idiom: unpacking to exactly 1 element raises
    # ValueError if 0 or 2+ events fired — no len(...) == N needed.
    (started_ev,) = started
    assert started_ev.get("agent_name") == "test-agent", (
        f"session_started.agent_name mismatch: {started_ev!r}"
    )

    (completed_ev,) = completed
    assert completed_ev.get("agent_name") == "test-agent", (
        f"session_completed.agent_name mismatch: {completed_ev!r}"
    )

    # session_started must appear before session_completed in the log
    all_types = [e["type"] for e in collected]
    idx_started = all_types.index("session_started")
    idx_completed = all_types.index("session_completed")
    assert idx_started < idx_completed, (
        f"session_started ({idx_started}) must precede session_completed "
        f"({idx_completed}) in the event log. Sequence: {all_types}"
    )


# ---------------------------------------------------------------------------
# Test 3: Tier 2 — turn_started fires once per turn in run_one_iteration()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_turn_started_emits_with_kind(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: turn_started is emitted once per turn in run_one_iteration(),
    carrying the inbox trigger's kind, before the turn's handler runs.

    Approach: inject a 'user' inbox message, then call run_one_iteration()
    once. #5103: drives the REAL `run_one_iteration` -> `_handle_user_
    message` -> `_run_router_loop` -> `RouterLoopDriver.run_turn` chain
    (architect design "C2", #5363's own precedent) — only the LLM boundary
    itself (`litellm.acompletion`) is stubbed via `@pytest.mark.llm_stub`,
    not `run_turn` wholesale. What this test asserts (turn_started fires
    with the right kind) was previously witnessed against a private
    `_noop` stand-in that replaced `run_turn` entirely; now it is
    witnessed on the real code path.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    collected = _collect_events(session)

    _chain_id = "test-chain-001"
    await session._put_inbox("user", {"text": "hello", "chain_id": _chain_id})
    result = await session.run_one_iteration()

    assert result is True, "run_one_iteration should return True (not shutdown)"

    started = _events_of_type(collected, "turn_started")

    # Unpack-enforcement idiom: exactly 1 turn_started must fire.
    (started_ev,) = started
    assert started_ev.get("kind") == "user", (
        f"turn_started.kind should be 'user', got: {started_ev!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: Tier 2 — turn_completed fires after RouterLoopDriver.run_turn() returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_turn_completed_emits_after_router_turn(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: turn_completed is emitted in _run_router_loop() immediately after
    RouterLoopDriver.run_turn() returns — the terminal stop_reason point.

    One turn_completed is emitted per turn. It carries the chain_id that
    matches the user message's chain_id (cross-agent tracing, P6).

    #5103 ④ migration: previously replaced `_loop_driver.run_turn` with a
    private closure that recorded how many events had been collected at
    the moment run_turn was CALLED, then asserted turn_completed's index
    was >= that count — a private mid-dispatch observation point. Now a
    REAL turn dispatches (`@pytest.mark.llm_stub`, only
    `litellm.acompletion` is stubbed) and the SAME "before/after run_turn"
    bracket is read off the public `stall_trace_armed`/
    `stall_trace_disarmed` audit-events (#5103 ④, this issue) — armed is
    the first statement inside `_run_turn_body`'s task, before run_turn is
    dispatched; disarmed is in the outermost-of-innermost `finally`, after
    run_turn has returned AND every subsequent boundary operation
    (turn_end hooks, hot-reload, journal cut) has too. REYN_STALL_TRACE is
    set purely to arm this observation pair — the N-second stall
    detection itself is not this test's subject (stall_trace.py's own
    docstring: no test exercises the actual firing, by design).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _make_session(tmp_path)
    collected = _collect_events(session)

    _chain_id = "test-chain-002"
    await session._put_inbox("user", {"text": "world", "chain_id": _chain_id})
    result = await session.run_one_iteration()

    assert result is True

    completed = _events_of_type(collected, "turn_completed")

    # Unpack-enforcement idiom: exactly 1 turn_completed must fire.
    (completed_ev,) = completed
    assert completed_ev.get("chain_id") == _chain_id, (
        f"turn_completed.chain_id should be {_chain_id!r}, got: {completed_ev!r}"
    )

    all_types = [e["type"] for e in collected]
    idx_armed = all_types.index("stall_trace_armed")
    idx_completed = next(
        i for i, e in enumerate(collected) if e["type"] == "turn_completed"
    )
    idx_disarmed = all_types.index("stall_trace_disarmed")
    # turn_completed must be strictly BETWEEN armed (before run_turn is
    # dispatched) and disarmed (after run_turn returns AND the whole
    # unwind chain completes) — proving it fires only after the terminal
    # stop_reason, never before.
    assert idx_armed < idx_completed < idx_disarmed, (
        f"turn_completed (idx {idx_completed}) must fall strictly between "
        f"stall_trace_armed (idx {idx_armed}) and stall_trace_disarmed "
        f"(idx {idx_disarmed}) — it must not appear before run_turn "
        f"dispatches or after the whole turn boundary has unwound. "
        f"Event sequence: {all_types}"
    )
