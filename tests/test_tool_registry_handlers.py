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

from reyn.tools.catalog import LIST_SKILLS, DESCRIBE_SKILL, LIST_AGENTS, DESCRIBE_AGENT
from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
from reyn.tools.plan import PLAN
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


# ── list_skills ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_skills_handler_delegates_to_router_state_fn():
    """Tier 2: list_skills handler delegates to ctx.router_state.list_skills_fn and wraps result."""
    captured_path: list[str] = []

    def fake_fn(path: str) -> list:
        captured_path.append(path)
        return [{"name": "s1", "description": "d1"}, {"name": "s2", "description": "d2"}]

    rs = RouterCallerState(list_skills_fn=fake_fn)
    result = await LIST_SKILLS.handler({"path": "write/blog"}, _ctx(rs))

    assert captured_path == ["write/blog"]
    assert result == [{"name": "s1", "description": "d1"}, {"name": "s2", "description": "d2"}]


@pytest.mark.asyncio
async def test_list_skills_handler_raises_when_router_state_none():
    """Tier 2: list_skills raises RuntimeError when router_state is None."""
    with pytest.raises(RuntimeError, match="router_state.list_skills_fn"):
        await LIST_SKILLS.handler({"path": ""}, _ctx(None))


@pytest.mark.asyncio
async def test_list_skills_handler_raises_when_fn_none():
    """Tier 2: list_skills raises RuntimeError when list_skills_fn is None."""
    rs = RouterCallerState()  # list_skills_fn defaults to None
    with pytest.raises(RuntimeError, match="router_state.list_skills_fn"):
        await LIST_SKILLS.handler({"path": ""}, _ctx(rs))


# ── describe_skill ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_describe_skill_handler_delegates_to_router_state_fn():
    """Tier 2: describe_skill handler delegates to ctx.router_state.describe_skill_fn and returns directly."""
    captured_name: list[str] = []

    def fake_fn(name: str) -> dict:
        captured_name.append(name)
        return {"name": name, "description": "writes a blog post", "when_to_use": "..."}

    rs = RouterCallerState(describe_skill_fn=fake_fn)
    result = await DESCRIBE_SKILL.handler({"name": "write/blog"}, _ctx(rs))

    assert captured_name == ["write/blog"]
    assert result == {"name": "write/blog", "description": "writes a blog post", "when_to_use": "..."}


@pytest.mark.asyncio
async def test_describe_skill_handler_raises_when_router_state_none():
    """Tier 2: describe_skill raises RuntimeError when router_state is None."""
    with pytest.raises(RuntimeError, match="router_state.describe_skill_fn"):
        await DESCRIBE_SKILL.handler({"name": "write/blog"}, _ctx(None))


@pytest.mark.asyncio
async def test_describe_skill_handler_raises_when_fn_none():
    """Tier 2: describe_skill raises RuntimeError when describe_skill_fn is None."""
    rs = RouterCallerState()  # describe_skill_fn defaults to None
    with pytest.raises(RuntimeError, match="router_state.describe_skill_fn"):
        await DESCRIBE_SKILL.handler({"name": "write/blog"}, _ctx(rs))


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


# ---------------------------------------------------------------------------
# Wave 2b: plan handler (ADR-0026 M4 Phase 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_handler_delegates_to_router_state_dispatch_plan_tool():
    """Tier 2: plan handler delegates to ctx.router_state.dispatch_plan_tool with args passthrough."""
    captured: list[dict] = []

    async def fake_dispatch(*, args):
        captured.append(args)
        return {"status": "dispatched", "plan_id": "p_abc"}

    rs = RouterCallerState(dispatch_plan_tool=fake_dispatch)
    ctx = _ctx(rs)
    plan_args = {"goal": "test", "steps_json": "[]"}
    result = await PLAN.handler(plan_args, ctx)
    assert captured == [plan_args]
    assert result == {"status": "dispatched", "plan_id": "p_abc"}


@pytest.mark.asyncio
async def test_plan_handler_raises_when_dispatch_plan_tool_missing():
    """Tier 2: plan handler raises RuntimeError when dispatch_plan_tool is not populated."""
    rs = RouterCallerState()  # dispatch_plan_tool defaults to None
    with pytest.raises(RuntimeError, match="dispatch_plan_tool"):
        await PLAN.handler({"goal": "test", "steps_json": "[]"}, _ctx(rs))


@pytest.mark.asyncio
async def test_plan_handler_raises_when_router_state_is_none():
    """Tier 2: plan handler raises RuntimeError when ctx.router_state is None."""
    with pytest.raises(RuntimeError, match="dispatch_plan_tool"):
        await PLAN.handler({"goal": "test", "steps_json": "[]"}, _ctx(None))


# ── delegate_to_agent ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delegate_to_agent_handler_delegates_to_send_to_agent():
    """Tier 2: delegate_to_agent handler delegates to ctx.router_state.send_to_agent
    with per-call args and returns the spawn-ack dict (chain_id is bound at
    population time, not by the handler)."""
    captured_kwargs: list[dict] = []

    async def fake_send(*, to: str, request: str) -> None:
        captured_kwargs.append({"to": to, "request": request})

    rs = RouterCallerState(send_to_agent=fake_send)
    ctx = _ctx(rs)
    result = await DELEGATE_TO_AGENT.handler(
        {"to": "peer_agent", "request": "please do X"}, ctx
    )
    assert captured_kwargs == [{"to": "peer_agent", "request": "please do X"}]
    assert result["status"] == "dispatched"
    assert result["to"] == "peer_agent"
    assert "future router invocation" in result["note"]


@pytest.mark.asyncio
async def test_delegate_to_agent_handler_raises_when_send_to_agent_missing():
    """Tier 2: delegate_to_agent handler raises RuntimeError when send_to_agent
    is not populated (= mis-wired dispatcher)."""
    rs = RouterCallerState()  # send_to_agent defaults to None
    with pytest.raises(RuntimeError, match="send_to_agent"):
        await DELEGATE_TO_AGENT.handler(
            {"to": "peer", "request": "hi"}, _ctx(rs)
        )


@pytest.mark.asyncio
async def test_delegate_to_agent_handler_raises_when_router_state_is_none():
    """Tier 2: delegate_to_agent handler raises RuntimeError when router_state is None."""
    with pytest.raises(RuntimeError, match="send_to_agent"):
        await DELEGATE_TO_AGENT.handler(
            {"to": "peer", "request": "hi"}, _ctx(None)
        )
