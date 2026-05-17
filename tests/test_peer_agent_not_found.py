"""Tier 2: delegate_to_agent returns error-shape for non-existent peer (B33 W5 F2).

B33 dogfood observed that invoke_action(action_name="agent.peer__researcher",
...) against a non-existent peer agent returned tool_returned: status="dispatched"
and the LLM then fabricated plausible content as if the peer had answered.

This module verifies the fix: when the requested peer name is absent from
RouterCallerState.available_agents, the handler must return an error-shaped
response (status="error", kind="agent_not_found") before calling send_to_agent.
"""
from __future__ import annotations

import pytest

from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
from reyn.tools.types import RouterCallerState, ToolContext

# ── helpers ───────────────────────────────────────────────────────────────────


def _ctx(rs: RouterCallerState | None) -> ToolContext:
    """Build a minimal ToolContext with the given RouterCallerState."""
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


# ── error-shape regression (B33 W5 F2 fix) ────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_not_in_registry_returns_error_shape():
    """Tier 2: handler returns status=error when peer is absent from available_agents.

    Root cause: delegate_to_agent.py _handle returned {status: 'dispatched'}
    unconditionally before calling send_to_agent, which is fire-and-forget
    (returns None).  The transport layer (session._a2a_send_request) put the
    'agent not found' error to outbox but the handler's return value was
    already success-shaped.

    Fix: check available_agents before dispatching; return error-shape if peer
    is missing so the LLM sees the failure instead of fabricating content.
    """
    send_called: list[dict] = []

    async def fake_send(*, to: str, request: str) -> None:
        send_called.append({"to": to, "request": request})

    rs = RouterCallerState(
        send_to_agent=fake_send,
        available_agents=[
            {"name": "planner", "role": "planning agent"},
            {"name": "reviewer", "role": "review agent"},
        ],
    )
    result = await DELEGATE_TO_AGENT.handler(
        {"to": "researcher", "request": "summarise FP-0001"},
        _ctx(rs),
    )

    # Must be error-shaped — not success-shaped.
    assert result["status"] == "error", (
        f"Expected error shape but got: {result!r}"
    )
    assert result["kind"] == "agent_not_found"
    assert "researcher" in result["error"]

    # available_agents field lists what IS available (for LLM recovery).
    assert "planner" in result["available_agents"]
    assert "reviewer" in result["available_agents"]

    # send_to_agent must NOT have been called — no dispatch to nonexistent peer.
    assert send_called == [], (
        f"send_to_agent should not be called for unknown peer, but got: {send_called}"
    )


@pytest.mark.asyncio
async def test_peer_not_in_empty_registry_returns_error_shape():
    """Tier 2: error-shape returned when available_agents is an empty list.

    Covers the common test scenario: AgentRegistry with no registered agents.
    """
    send_called: list[dict] = []

    async def fake_send(*, to: str, request: str) -> None:
        send_called.append({"to": to, "request": request})

    rs = RouterCallerState(
        send_to_agent=fake_send,
        available_agents=[],  # no agents registered
    )
    result = await DELEGATE_TO_AGENT.handler(
        {"to": "nonexistent", "request": "hello"},
        _ctx(rs),
    )

    assert result["status"] == "error"
    assert result["kind"] == "agent_not_found"
    assert "nonexistent" in result["error"]
    assert result["available_agents"] == []
    assert send_called == []


@pytest.mark.asyncio
async def test_peer_not_in_registry_available_agents_none_still_dispatches():
    """Tier 2: when available_agents is None, handler skips the existence check.

    available_agents=None means the caller state was not fully populated
    (= test stubs, legacy paths, partial wiring).  In that case the handler
    falls through to send_to_agent to preserve the legacy behavior — not
    silently breaking partially-wired callers.
    """
    send_called: list[dict] = []

    async def fake_send(*, to: str, request: str) -> None:
        send_called.append({"to": to, "request": request})

    rs = RouterCallerState(
        send_to_agent=fake_send,
        available_agents=None,  # not populated — skip existence check
    )
    result = await DELEGATE_TO_AGENT.handler(
        {"to": "unknown_peer", "request": "fallthrough"},
        _ctx(rs),
    )

    # Should dispatch (legacy fallthrough when list is unknown).
    assert result["status"] == "dispatched"
    assert result["to"] == "unknown_peer"
    assert send_called == [{"to": "unknown_peer", "request": "fallthrough"}]


# ── happy path still passes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peer_exists_in_registry_dispatches_successfully():
    """Tier 2: handler dispatches and returns status=dispatched when peer IS in available_agents.

    Guards against the fix accidentally breaking the happy path (= existing
    peer agents that are properly registered).
    """
    send_called: list[dict] = []

    async def fake_send(*, to: str, request: str) -> None:
        send_called.append({"to": to, "request": request})

    rs = RouterCallerState(
        send_to_agent=fake_send,
        available_agents=[
            {"name": "researcher", "role": "research agent"},
        ],
    )
    result = await DELEGATE_TO_AGENT.handler(
        {"to": "researcher", "request": "find info on FP-0001"},
        _ctx(rs),
    )

    assert result["status"] == "dispatched"
    assert result["to"] == "researcher"
    assert "future router invocation" in result["note"]
    assert send_called == [{"to": "researcher", "request": "find info on FP-0001"}]
