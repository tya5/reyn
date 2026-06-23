"""Tests for #1800 slice 4b: wake=false ride-along (C) next-turn-context staging.

Four test groups:

1. **Tier 1 (contract) — trigger-turn history injection**: ride-alongs drained
   before a wake=true trigger appear as attributed system-role ChatMessages in
   history before the trigger's user message; ``_next_turn_context`` is cleared
   after injection.

2. **Tier 2 (OS invariant — B=persist durability)**: C messages are staged
   durably (WAL + snapshot) DURING ``_drain_to_wake`` — before the trigger
   arrives — so a crash during the blocking wait does not lose them.
   restore_state recovers them in ``_next_turn_context``.

3. **Tier 1 (contract) — slash/intervention short-circuit safety**: a slash
   command short-circuit in ``_handle_user_message`` does NOT consume the staged
   C messages; they wait for the real turn.

4. **Tier 2 (OS invariant — B=persist crash-window)**: falsifiable proof that
   the B=persist gap is closed — C is durable DURING the drain-wait, not only
   after ``run_one_iteration`` returns (the pre-fix location).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock.MagicMock / AsyncMock / patch to fake collaborators.
- Observations flow through: history list, StateLog.iter_from(), AgentSnapshot.load().
- Each test docstring first line declares its Tier.
- Count assertions use variable-binding idiom.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _text_result(text: str = "ok") -> LLMToolCallResult:
    """Real LLMToolCallResult that makes RouterLoop emit a text reply."""
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    """Build a minimal Session with WAL + snapshot path."""
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _wal_events(tmp_path: Path) -> list[dict]:
    """Read all WAL events from the session state log."""
    log = StateLog(tmp_path / "state.wal")
    return list(log.iter_from(0))


def _make_llm_stub_fn(result):  # type: ignore[no-untyped-def]
    """Return a real async callable that mimics call_llm_tools. No mocks."""
    if isinstance(result, list):
        results = list(result)
        idx = [0]

        async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
            i = idx[0]
            idx[0] += 1
            return results[i] if i < len(results) else results[-1]

        return _stub

    async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
        return result

    return _stub


async def _run_n_turns_then_shutdown(
    session: Session,
    n: int,
    timeout: float = 5.0,
) -> None:
    """Run the session loop until n turns complete, then shutdown."""
    turns_done = [0]
    original_run_one = session.run_one_iteration

    async def _counting_run_one() -> bool:
        result = await original_run_one()
        if result:
            turns_done[0] += 1
        return result

    session.run_one_iteration = _counting_run_one  # type: ignore[method-assign]

    run_task = asyncio.create_task(session.run())
    deadline = asyncio.get_event_loop().time() + timeout
    while turns_done[0] < n:
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.005)
    await session.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except asyncio.TimeoutError:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Group 1 — Tier 1: trigger-turn history injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_staging_ride_alongs_injected_as_system_messages(
    tmp_path, monkeypatch,
) -> None:
    """Tier 1: [wake=false, wake=false, wake=true] → C's injected as system messages.

    Contract: when a wake=true trigger is preceded by 2 wake=false ride-alongs,
    the trigger's turn history contains the 2 C's as attributed system-role
    ChatMessages before the trigger's user message.  ``_next_turn_context``
    is empty after the turn.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    stub = _make_llm_stub_fn(_text_result("got it"))
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    # Two wake=false ride-alongs, then one wake=true trigger.
    await session._put_inbox("hook", {"text": "context-a", "name": "hook_a", "wake": False})
    await session._put_inbox("hook", {"text": "context-b", "name": "hook_b", "wake": False})
    await session._put_inbox("user", {"text": "trigger-msg", "wake": True})

    await _run_n_turns_then_shutdown(session, n=1)

    # Find the system messages in history.
    system_msgs = [m for m in session.history if m.role == "system"]

    # There must be at least 2 system-role messages from the ride-alongs.
    n_sys = len(system_msgs)
    assert n_sys >= 2, (
        f"Expected at least 2 system-role messages from ride-alongs; got {n_sys}: "
        f"{[m.content for m in system_msgs]}"
    )

    # The first two must be attributed with [hook:hook_a] and [hook:hook_b].
    sys_a, sys_b = system_msgs[0], system_msgs[1]
    assert "[hook:hook_a]" in sys_a.content, (
        f"First system message must be attributed [hook:hook_a]; got {sys_a.content!r}"
    )
    assert "[hook:hook_b]" in sys_b.content, (
        f"Second system message must be attributed [hook:hook_b]; got {sys_b.content!r}"
    )
    assert "context-a" in sys_a.content, (
        f"First system message must contain 'context-a'; got {sys_a.content!r}"
    )
    assert "context-b" in sys_b.content, (
        f"Second system message must contain 'context-b'; got {sys_b.content!r}"
    )

    # The system messages must appear BEFORE the trigger's user message in history.
    user_msgs = [m for m in session.history if m.role == "user"]
    n_user = len(user_msgs)
    assert n_user >= 1, f"Expected at least 1 user message; got {n_user}"

    last_user = user_msgs[-1]
    last_sys = system_msgs[-1]
    history_roles = [m.role for m in session.history]
    sys_idx = session.history.index(last_sys)
    user_idx = session.history.index(last_user)
    assert sys_idx < user_idx, (
        f"System messages (idx={sys_idx}) must appear before trigger user message "
        f"(idx={user_idx}); history roles: {history_roles}"
    )

    # _next_turn_context must be empty after the turn.
    n_ntc = len(session._next_turn_context)
    assert n_ntc == 0, (
        f"_next_turn_context must be empty after turn; got {n_ntc} entries"
    )


@pytest.mark.asyncio
async def test_c_staging_no_ride_alongs_no_system_messages(
    tmp_path, monkeypatch,
) -> None:
    """Tier 1: wake=true trigger with no preceding ride-alongs → no extra system messages.

    The common path (no ride-alongs) must not inject any system-role messages
    from the staging buffer.  Back-compat: existing turn behaviour unchanged.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    stub = _make_llm_stub_fn(_text_result("hello back"))
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    await session.submit_user_text("plain message")
    await _run_n_turns_then_shutdown(session, n=1)

    system_msgs = [m for m in session.history if m.role == "system"]
    n_sys = len(system_msgs)
    assert n_sys == 0, (
        f"Expected no system-role messages from ride-alongs; got {n_sys}: "
        f"{[m.content for m in system_msgs]}"
    )


# ---------------------------------------------------------------------------
# Group 2 — Tier 2: B=persist durability — snapshot survives crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_staging_persist_and_restore(tmp_path) -> None:
    """Tier 2: (OS invariant — B=persist): staged C's survive snapshot+restore.

    Contract (decision B): staging a wake=false ride-along writes it to the
    WAL + snapshot durably.  A fresh Session restored from that snapshot
    recovers the entries in ``_next_turn_context``.

    Falsifiable: without persistence, restore_state would leave
    ``_next_turn_context`` empty and this test would fail.
    """
    session = _make_session(tmp_path)

    # Directly call the journal method to stage a ride-along entry.
    await session._journal.record_next_turn_context_staged(
        kind="hook",
        payload={"name": "hook_a", "text": "ride-along context"},
    )

    # Verify the snapshot file contains the staged entry.
    snapshot_on_disk = AgentSnapshot.load(
        session.agent_name, session._snapshot_path,
    )
    n_ntc = len(snapshot_on_disk.next_turn_context)
    assert n_ntc == 1, (
        f"Snapshot must contain 1 next_turn_context entry after staging; got {n_ntc}"
    )
    (staged_entry,) = snapshot_on_disk.next_turn_context
    assert staged_entry.get("kind") == "hook", (
        f"Staged entry kind must be 'hook'; got {staged_entry!r}"
    )
    assert staged_entry.get("payload", {}).get("text") == "ride-along context", (
        f"Staged entry payload text must be 'ride-along context'; got {staged_entry!r}"
    )

    # Simulate crash+restore: build a new Session and restore from the snapshot.
    session2 = _make_session(tmp_path, agent_name="test_agent2")
    # Reuse the same snapshot path as session (same agent_name — but load the snap directly).
    recovered_snap = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    # Manually install the recovered snapshot's next_turn_context into session2.
    # (In production, restore_state does this; we test restore_state directly below.)
    session3 = _make_session(tmp_path, agent_name="test_agent3")
    # Build a minimal snapshot with the staged entry.
    restored_snap = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    # Call restore_state on a new session (same agent_name, same snapshot_path).
    session4 = Session(
        agent_name=session.agent_name,
        state_log=StateLog(tmp_path / "state2.wal"),
        snapshot_path=session._snapshot_path,
    )
    # restore_state expects the snapshot to already have next_turn_context.
    session4.restore_state(restored_snap)

    n_recovered = len(session4._next_turn_context)
    assert n_recovered == 1, (
        f"restore_state must recover 1 next_turn_context entry; got {n_recovered}"
    )
    (recovered_entry,) = session4._next_turn_context
    assert recovered_entry.get("kind") == "hook", (
        f"Recovered entry kind must be 'hook'; got {recovered_entry!r}"
    )
    assert recovered_entry.get("payload", {}).get("text") == "ride-along context", (
        f"Recovered entry payload text; got {recovered_entry!r}"
    )

    # Verify WAL has next_turn_context_staged event.
    events = _wal_events(tmp_path)
    staged_events = [e for e in events if e.get("kind") == "next_turn_context_staged"]
    n_staged = len(staged_events)
    assert n_staged == 1, (
        f"Expected 1 next_turn_context_staged WAL event; got {n_staged}"
    )


@pytest.mark.asyncio
async def test_c_staging_cleared_durably_after_injection(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: (OS invariant — B=persist): cleared durably → snapshot empty after turn.

    After the trigger turn applies the staged entries, ``_next_turn_context``
    is cleared durably in the snapshot.  A crash after the turn would NOT
    re-inject the C messages on the next restart.

    Falsifiable: without the clear WAL event, the snapshot would still hold
    the entries after the turn and re-injection would occur on restore.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    stub = _make_llm_stub_fn(_text_result("done"))
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    await session._put_inbox("hook", {"text": "ctx", "name": "my_hook", "wake": False})
    await session._put_inbox("user", {"text": "trigger", "wake": True})

    await _run_n_turns_then_shutdown(session, n=1)

    # WAL must contain next_turn_context_cleared event.
    events = _wal_events(tmp_path)
    cleared_events = [e for e in events if e.get("kind") == "next_turn_context_cleared"]
    n_cleared = len(cleared_events)
    assert n_cleared == 1, (
        f"Expected 1 next_turn_context_cleared WAL event after turn; got {n_cleared}"
    )

    # Snapshot must have empty next_turn_context.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    n_ntc = len(snapshot.next_turn_context)
    assert n_ntc == 0, (
        f"Snapshot next_turn_context must be empty after turn; got {n_ntc}"
    )


@pytest.mark.asyncio
async def test_c_staging_durable_during_drain_wait(tmp_path) -> None:
    """Tier 2: (OS invariant — B=persist): C staged in _drain_to_wake BEFORE trigger arrives.

    Falsifiable proof that the B=persist gap is fixed: a wake=false C is
    persisted (WAL + snapshot) DURING _drain_to_wake — while the drain is
    still blocking waiting for the trigger — NOT after run_one_iteration
    returns.  A crash during that blocking wait must not lose the C.

    Mechanism under test: _drain_to_wake calls record_next_turn_context_staged
    immediately after consuming a wake=false message, before re-entering the
    blocking wait for the trigger.

    Falsification: without the fix (staging only in run_one_iteration), the
    WAL + snapshot would be empty while _drain_to_wake is blocked, and this
    test would fail on the staged_events / n_snap assertions.

    Test structure:
    1. Enqueue one wake=false C into the inbox (no trigger).
    2. Start _drain_to_wake as a concurrent task — it consumes the C, stages
       it durably, then blocks waiting for the trigger (Decision A).
    3. Yield briefly so the task can consume and stage the C.
    4. While still blocking (no trigger sent), verify WAL + snapshot already
       hold the staged entry.
    5. Simulate crash+restore: load the snapshot into a new Session via
       restore_state; verify _next_turn_context is recovered.
    6. Cancel the drain task (simulating the crash).
    """
    session = _make_session(tmp_path)

    # Enqueue one wake=false C — no trigger follows.
    await session._put_inbox(
        "hook", {"name": "pre_trigger_hook", "text": "crash-window-context", "wake": False},
    )

    # Start _drain_to_wake concurrently.  It will consume the wake=false C,
    # stage it durably, then block waiting for a trigger (Decision A).
    drain_task = asyncio.create_task(session._drain_to_wake())

    # Yield control briefly to let the drain task consume the C and call
    # record_next_turn_context_staged.  Multiple yields improve reliability
    # across different event-loop scheduling policies.
    for _ in range(10):
        await asyncio.sleep(0)

    # While _drain_to_wake is still blocking (no trigger sent), verify
    # durability: WAL + snapshot must already contain the staged entry.
    events = _wal_events(tmp_path)
    staged_events = [e for e in events if e.get("kind") == "next_turn_context_staged"]
    n_staged = len(staged_events)
    assert n_staged == 1, (
        f"Expected 1 next_turn_context_staged WAL event DURING drain wait (no trigger sent); "
        f"got {n_staged}.  Without the fix, staging happens only after _drain_to_wake "
        f"returns (in run_one_iteration), so the WAL would still be empty here."
    )

    snap = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    n_snap = len(snap.next_turn_context)
    assert n_snap == 1, (
        f"Snapshot must hold 1 next_turn_context entry DURING drain wait; got {n_snap}"
    )
    (snap_entry,) = snap.next_turn_context
    assert snap_entry.get("kind") == "hook", (
        f"Snapshot entry kind must be 'hook'; got {snap_entry!r}"
    )
    assert snap_entry.get("payload", {}).get("text") == "crash-window-context", (
        f"Snapshot entry payload text must be 'crash-window-context'; got {snap_entry!r}"
    )

    # Simulate crash: cancel the drain task (= process killed while blocking).
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)

    # Simulate restore: a fresh Session restores from the snapshot.
    # restore_state must recover the staged entry in _next_turn_context.
    session2 = Session(
        agent_name=session.agent_name,
        state_log=StateLog(tmp_path / "state2.wal"),
        snapshot_path=session._snapshot_path,
    )
    recovered_snap = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    session2.restore_state(recovered_snap)

    n_recovered = len(session2._next_turn_context)
    assert n_recovered == 1, (
        f"restore_state must recover 1 next_turn_context entry after crash; "
        f"got {n_recovered}"
    )
    (recovered_entry,) = session2._next_turn_context
    assert recovered_entry.get("kind") == "hook", (
        f"Recovered entry kind must be 'hook'; got {recovered_entry!r}"
    )
    assert recovered_entry.get("payload", {}).get("text") == "crash-window-context", (
        f"Recovered entry payload text; got {recovered_entry!r}"
    )


# ---------------------------------------------------------------------------
# Group 3 — Tier 1: slash/intervention short-circuit safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_staging_slash_command_does_not_consume_staged(
    tmp_path, monkeypatch,
) -> None:
    """Tier 1: slash command short-circuit does NOT consume staged C messages.

    Contract (flow-trace §3 risk note): a slash command in
    ``_handle_user_message`` short-circuits before the injection point.
    The staged C messages must still be in ``_next_turn_context`` after
    the slash command returns (they wait for a real turn).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Stage a C message manually.
    session._next_turn_context.append(
        {"kind": "hook", "payload": {"name": "pending_hook", "text": "pending-context"}}
    )

    # Call _handle_user_message with a slash command (e.g. /help, /list).
    # We don't need LLM stubs since slash commands short-circuit before the LLM.
    await session._handle_user_message("/help", chain_id="test-chain")

    # The staged entries must still be in _next_turn_context.
    n_ntc = len(session._next_turn_context)
    assert n_ntc == 1, (
        f"Slash command must NOT consume staged C messages; "
        f"expected 1 entry in _next_turn_context, got {n_ntc}"
    )
