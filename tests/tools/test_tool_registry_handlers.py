"""Tier 2 invariant tests for catalog ToolDefinition handlers (ADR-0026 M4 Phase 3).

Each test verifies that a catalog handler:
  1. Delegates to the typed RouterCallerState callable field.
  2. Returns the correct shape — list handlers return list directly
     (= byte-identity with legacy router branches; LLMReplay safety),
     describe handlers return the Mapping directly.
  3. Raises RuntimeError with a descriptive message when router_state
     or the relevant fn field is None (mis-wired or test site omission).
"""
from __future__ import annotations

import pytest

from reyn.tools.catalog import DESCRIBE_AGENT, LIST_AGENTS
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


# ── list_agents ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_agents_handler_delegates_to_router_state_fn():
    """Tier 2: list_agents handler delegates to ctx.router_state.list_agents_fn and wraps result."""
    captured_path: list[str] = []

    def fake_fn(path: str) -> list:
        captured_path.append(path)
        return [{"name": "research", "description": "research agent"}]

    rs = RouterCallerState(list_agents_fn=fake_fn)
    result = await LIST_AGENTS.handler({"path": "cluster-a"}, _ctx(rs))

    assert captured_path == ["cluster-a"]
    assert result == [{"name": "research", "description": "research agent"}]


@pytest.mark.asyncio
async def test_list_agents_handler_raises_when_router_state_none():
    """Tier 2: list_agents raises RuntimeError when router_state is None."""
    with pytest.raises(RuntimeError, match="router_state.list_agents_fn"):
        await LIST_AGENTS.handler({"path": ""}, _ctx(None))


@pytest.mark.asyncio
async def test_list_agents_handler_raises_when_fn_none():
    """Tier 2: list_agents raises RuntimeError when list_agents_fn is None."""
    rs = RouterCallerState()  # list_agents_fn defaults to None
    with pytest.raises(RuntimeError, match="router_state.list_agents_fn"):
        await LIST_AGENTS.handler({"path": ""}, _ctx(rs))


# ── describe_agent ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_describe_agent_handler_delegates_to_router_state_fn():
    """Tier 2: describe_agent handler delegates to ctx.router_state.describe_agent_fn and returns directly."""
    captured_name: list[str] = []

    def fake_fn(name: str) -> dict:
        captured_name.append(name)
        return {"name": name, "role": "research specialist", "capabilities": ["web_search"]}

    rs = RouterCallerState(describe_agent_fn=fake_fn)
    result = await DESCRIBE_AGENT.handler({"name": "research"}, _ctx(rs))

    assert captured_name == ["research"]
    assert result == {"name": "research", "role": "research specialist", "capabilities": ["web_search"]}


@pytest.mark.asyncio
async def test_describe_agent_handler_raises_when_router_state_none():
    """Tier 2: describe_agent raises RuntimeError when router_state is None."""
    with pytest.raises(RuntimeError, match="router_state.describe_agent_fn"):
        await DESCRIBE_AGENT.handler({"name": "research"}, _ctx(None))


@pytest.mark.asyncio
async def test_describe_agent_handler_raises_when_fn_none():
    """Tier 2: describe_agent raises RuntimeError when describe_agent_fn is None."""
    rs = RouterCallerState()  # describe_agent_fn defaults to None
    with pytest.raises(RuntimeError, match="router_state.describe_agent_fn"):
        await DESCRIBE_AGENT.handler({"name": "research"}, _ctx(rs))
