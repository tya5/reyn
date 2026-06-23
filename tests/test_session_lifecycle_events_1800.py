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
- Only the LLM-calling boundary (_loop_driver.run_turn, which invokes
  RouterLoop → call_llm_tools) is replaced with a plain async callable,
  consistent with the "LLM is the only collaborator that may be faked"
  rule (Tier 2c).
- Events observed via add_subscriber (public EventLog API), not via
  private state assertions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test-agent") -> Session:
    """Build a minimal Session wired to tmp_path."""
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _collect_events(session: Session) -> list[dict]:
    """Subscribe a collector to the session's EventLog and return the list.

    The returned list is mutated in-place as events arrive (subscriber is
    called synchronously in emit(), so the list is always current).
    """
    collected: list[dict] = []

    def _subscriber(event) -> None:
        collected.append({"type": event.type, **event.data})

    # add_subscriber is the public API on EventLog; _chat_events is the
    # session's internal EventLog that all session-level emits target.
    session._chat_events.add_subscriber(_subscriber)
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
async def test_turn_started_emits_with_kind(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: turn_started is emitted once per turn in run_one_iteration(),
    carrying the inbox trigger's kind, before the turn's handler runs.

    Approach: inject a 'user' inbox message, monkeypatch _loop_driver.run_turn
    to a fast async noop (LLM-boundary fake — the only collaborator the policy
    allows faking in Tier 2c), then call run_one_iteration() once.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    collected = _collect_events(session)

    # Replace the LLM-calling boundary (run_turn) with a plain async noop.
    # This is the only fake; the real run_one_iteration → _handle_user_message
    # → _run_router_loop chain runs.
    async def _noop_run_turn(user_text: str, chain_id: str) -> None:
        pass

    session._loop_driver.run_turn = _noop_run_turn  # type: ignore[method-assign]

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
async def test_turn_completed_emits_after_router_turn(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: turn_completed is emitted in _run_router_loop() immediately after
    RouterLoopDriver.run_turn() returns — the terminal stop_reason point.

    One turn_completed is emitted per turn. It carries the chain_id that
    matches the user message's chain_id (cross-agent tracing, P6).

    Same LLM-boundary fake approach as test_turn_started_emits_with_kind.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    collected = _collect_events(session)

    # Track order: record chain_id when run_turn is called, then verify
    # turn_completed fires after (via event list ordering).
    run_turn_called_at: list[int] = []

    async def _noop_run_turn(user_text: str, chain_id: str) -> None:
        run_turn_called_at.append(len(collected))

    session._loop_driver.run_turn = _noop_run_turn  # type: ignore[method-assign]

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

    # turn_completed must appear after run_turn was called (= after the
    # terminal stop_reason, not before).
    all_types = [e["type"] for e in collected]
    idx_completed = next(
        i for i, e in enumerate(collected) if e["type"] == "turn_completed"
    )
    assert run_turn_called_at, "run_turn was never called"
    run_turn_event_count = run_turn_called_at[0]
    # turn_completed is at position idx_completed (0-based) in the event
    # list. run_turn_event_count events were logged before run_turn
    # returned. turn_completed must be at position >= run_turn_event_count
    # (= it fires at or after the point where run_turn returned, never
    # before).
    assert idx_completed >= run_turn_event_count, (
        f"turn_completed (at event idx {idx_completed}) must NOT appear before "
        f"run_turn returns (run_turn saw {run_turn_event_count} events). "
        f"Event sequence: {all_types}"
    )
