"""Tier 2 invariants for the unified tool registry (ADR-0026 M1).

Tests cover ToolDefinition, ToolGates, ToolContext, ToolRegistry, and the
invoke_tool dispatch helper. No mocks of collaborators — all tests use real
ToolDefinition / ToolRegistry instances and closure-based test handlers.
No private state assertions — all assertions use public accessors (lookup,
names, for_router, for_phase, len, __contains__).
"""
from __future__ import annotations

import dataclasses
import pytest

from reyn.tools import (
    ToolDefinition,
    ToolGates,
    ToolContext,
    RouterCallerState,
    PhaseCallerState,
    ToolRegistry,
)
from reyn.tools.dispatch import invoke_tool, ToolNotFound


# ── Shared fixtures ───────────────────────────────────────────────────────────

def _noop_handler():
    """Return a minimal async ToolHandler that records calls."""
    calls: list[tuple] = []

    async def handler(args, ctx):
        calls.append((args, ctx))
        return {"status": "ok"}

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def _make_tool(
    name: str = "test_tool",
    *,
    gates: ToolGates | None = None,
    category: str = "io",
    purity: str = "side_effect",
) -> ToolDefinition:
    """Build a minimal ToolDefinition for tests."""
    return ToolDefinition(
        name=name,
        description=f"Test tool: {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        gates=gates or ToolGates(),
        handler=_noop_handler(),
        category=category,
        purity=purity,  # type: ignore[arg-type]
    )


def _make_context(caller_kind: str = "router") -> ToolContext:
    """Build a minimal ToolContext with sentinel values."""

    class _SentinelEvents:
        pass

    class _SentinelWorkspace:
        pass

    return ToolContext(
        events=_SentinelEvents(),
        permission_resolver=None,
        workspace=_SentinelWorkspace(),
        caller_kind=caller_kind,  # type: ignore[arg-type]
    )


# ── 1. ToolDefinition is frozen ───────────────────────────────────────────────

def test_tool_definition_is_frozen():
    """Tier 2: ToolDefinition is a frozen dataclass; mutation raises FrozenInstanceError."""
    tool = _make_tool()
    with pytest.raises(dataclasses.FrozenInstanceError):
        tool.name = "mutated"  # type: ignore[misc]


# ── 2. ToolGates defaults ─────────────────────────────────────────────────────

def test_tool_gates_defaults():
    """Tier 2: ToolGates defaults to router=allow, phase=allow."""
    gates = ToolGates()
    assert gates.router == "allow"
    assert gates.phase == "allow"


def test_tool_gates_explicit_values():
    """Tier 2: ToolGates accepts explicit allow/deny values."""
    g_router_deny = ToolGates(router="deny", phase="allow")
    g_phase_deny = ToolGates(router="allow", phase="deny")
    assert g_router_deny.router == "deny"
    assert g_router_deny.phase == "allow"
    assert g_phase_deny.router == "allow"
    assert g_phase_deny.phase == "deny"


def test_tool_gates_is_frozen():
    """Tier 2: ToolGates is a frozen dataclass; mutation raises FrozenInstanceError."""
    gates = ToolGates()
    with pytest.raises(dataclasses.FrozenInstanceError):
        gates.router = "deny"  # type: ignore[misc]


# ── 3. render_for_router output shape ────────────────────────────────────────

def test_render_for_router_shape():
    """Tier 2: render_for_router() produces the exact OpenAI tools[] entry shape."""
    params = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    tool = ToolDefinition(
        name="my_tool",
        description="A test tool.",
        parameters=params,
        gates=ToolGates(),
        handler=_noop_handler(),
        category="io",
    )
    rendered = tool.render_for_router()

    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)
    fn = rendered["function"]
    assert fn["name"] == "my_tool"
    assert fn["description"] == "A test tool."
    assert fn["parameters"] == params


def test_render_for_router_parameters_is_dict():
    """Tier 2: render_for_router() parameters value is a plain dict (not Mapping)."""
    tool = _make_tool()
    rendered = tool.render_for_router()
    assert type(rendered["function"]["parameters"]) is dict


# ── 4. render_for_phase output shape ─────────────────────────────────────────

def test_render_for_phase_shape():
    """Tier 2: render_for_phase() produces a dict with kind, description, args_schema, purity."""
    params = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    tool = ToolDefinition(
        name="phase_tool",
        description="A phase tool.",
        parameters=params,
        gates=ToolGates(),
        handler=_noop_handler(),
        category="io",
        purity="side_effect",
    )
    rendered = tool.render_for_phase()

    assert rendered["kind"] == "phase_tool"
    assert rendered["description"] == "A phase tool."
    assert rendered["args_schema"] == params
    assert rendered["purity"] == "side_effect"


def test_render_for_phase_args_schema_is_dict():
    """Tier 2: render_for_phase() args_schema is a plain dict (not Mapping)."""
    tool = _make_tool()
    rendered = tool.render_for_phase()
    assert type(rendered["args_schema"]) is dict


# ── 5. purity defaults ────────────────────────────────────────────────────────

def test_tool_definition_purity_defaults_to_side_effect():
    """Tier 2: ToolDefinition.purity defaults to 'side_effect' when not specified."""
    tool = ToolDefinition(
        name="default_purity",
        description="Default purity tool.",
        parameters={"type": "object", "properties": {}, "required": []},
        gates=ToolGates(),
        handler=_noop_handler(),
        category="io",
    )
    assert tool.purity == "side_effect"


# ── 6. ToolRegistry register / lookup round-trip ─────────────────────────────

def test_registry_register_and_lookup():
    """Tier 2: ToolRegistry.register + lookup round-trip returns the same definition."""
    registry = ToolRegistry()
    tool = _make_tool("round_trip_tool")
    registry.register(tool)

    retrieved = registry.lookup("round_trip_tool")
    assert retrieved is tool


def test_registry_lookup_unknown_returns_none():
    """Tier 2: ToolRegistry.lookup returns None for an unregistered name."""
    registry = ToolRegistry()
    assert registry.lookup("does_not_exist") is None


def test_registry_names_reflects_registered():
    """Tier 2: ToolRegistry.names() lists all registered tool names."""
    registry = ToolRegistry()
    registry.register(_make_tool("tool_a"))
    registry.register(_make_tool("tool_b"))

    names = registry.names()
    assert "tool_a" in names
    assert "tool_b" in names
    assert len(names) == 2


def test_registry_contains():
    """Tier 2: ToolRegistry.__contains__ returns True for registered names."""
    registry = ToolRegistry()
    registry.register(_make_tool("present"))

    assert "present" in registry
    assert "absent" not in registry


def test_registry_len():
    """Tier 2: len(ToolRegistry) returns the count of registered tools."""
    registry = ToolRegistry()
    assert len(registry) == 0
    registry.register(_make_tool("one"))
    assert len(registry) == 1
    registry.register(_make_tool("two"))
    assert len(registry) == 2


def test_registry_iter():
    """Tier 2: Iterating a ToolRegistry yields all registered ToolDefinitions."""
    registry = ToolRegistry()
    t1 = _make_tool("iter_a")
    t2 = _make_tool("iter_b")
    registry.register(t1)
    registry.register(t2)

    all_tools = list(registry)
    assert t1 in all_tools
    assert t2 in all_tools
    assert len(all_tools) == 2


# ── 7. ToolRegistry duplicate name raises ────────────────────────────────────

def test_registry_duplicate_name_raises():
    """Tier 2: Registering a tool name that already exists raises ValueError."""
    registry = ToolRegistry()
    registry.register(_make_tool("dup"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_make_tool("dup"))


# ── 8. for_router filters by gates.router == "allow" ─────────────────────────

def test_for_router_filters_correctly():
    """Tier 2: ToolRegistry.for_router() returns only tools with gates.router=allow."""
    registry = ToolRegistry()
    router_tool = _make_tool("router_only", gates=ToolGates(router="allow", phase="allow"))
    denied_tool = _make_tool("router_denied", gates=ToolGates(router="deny", phase="allow"))
    registry.register(router_tool)
    registry.register(denied_tool)

    router_list = registry.for_router()
    assert router_tool in router_list
    assert denied_tool not in router_list


def test_for_router_empty_when_all_denied():
    """Tier 2: for_router() returns empty list when all tools have router=deny."""
    registry = ToolRegistry()
    registry.register(_make_tool("t1", gates=ToolGates(router="deny")))
    registry.register(_make_tool("t2", gates=ToolGates(router="deny")))
    assert registry.for_router() == []


# ── 9. for_phase filters by gates.phase == "allow" ───────────────────────────

def test_for_phase_filters_correctly():
    """Tier 2: ToolRegistry.for_phase() returns only tools with gates.phase=allow."""
    registry = ToolRegistry()
    phase_tool = _make_tool("phase_only", gates=ToolGates(router="allow", phase="allow"))
    denied_tool = _make_tool("phase_denied", gates=ToolGates(router="allow", phase="deny"))
    registry.register(phase_tool)
    registry.register(denied_tool)

    phase_list = registry.for_phase()
    assert phase_tool in phase_list
    assert denied_tool not in phase_list


def test_for_phase_empty_when_all_denied():
    """Tier 2: for_phase() returns empty list when all tools have phase=deny."""
    registry = ToolRegistry()
    registry.register(_make_tool("t1", gates=ToolGates(phase="deny")))
    registry.register(_make_tool("t2", gates=ToolGates(phase="deny")))
    assert registry.for_phase() == []


# ── 10. invoke_tool raises ToolNotFound on unknown name ──────────────────────

@pytest.mark.asyncio
async def test_invoke_tool_raises_tool_not_found():
    """Tier 2: invoke_tool raises ToolNotFound when the tool name is not registered."""
    registry = ToolRegistry()
    ctx = _make_context("router")

    with pytest.raises(ToolNotFound):
        await invoke_tool(registry, "nonexistent", {}, ctx)


# ── 11. invoke_tool invokes the handler with args + ctx ──────────────────────

@pytest.mark.asyncio
async def test_invoke_tool_calls_handler():
    """Tier 2: invoke_tool looks up the tool and calls its handler with args + ctx."""
    registry = ToolRegistry()
    handler = _noop_handler()
    tool = ToolDefinition(
        name="callable_tool",
        description="A callable test tool.",
        parameters={"type": "object", "properties": {}, "required": []},
        gates=ToolGates(),
        handler=handler,
        category="io",
    )
    registry.register(tool)

    ctx = _make_context("router")
    args = {"key": "value"}
    result = await invoke_tool(registry, "callable_tool", args, ctx)

    assert result == {"status": "ok"}
    assert len(handler.calls) == 1
    called_args, called_ctx = handler.calls[0]
    assert called_args == args
    assert called_ctx is ctx


@pytest.mark.asyncio
async def test_invoke_tool_returns_handler_result():
    """Tier 2: invoke_tool returns the ToolResult produced by the handler."""

    async def custom_handler(args, ctx):
        return {"output": "custom_result", "echo": args.get("input")}

    registry = ToolRegistry()
    tool = ToolDefinition(
        name="result_tool",
        description="Returns custom result.",
        parameters={"type": "object", "properties": {}, "required": []},
        gates=ToolGates(),
        handler=custom_handler,
        category="io",
    )
    registry.register(tool)

    ctx = _make_context("phase")
    result = await invoke_tool(registry, "result_tool", {"input": "hello"}, ctx)

    assert result["output"] == "custom_result"
    assert result["echo"] == "hello"


# ── 12. Gate partitioning — router=allow+phase=deny vs router=deny+phase=allow ──

def test_gate_partitioning_router_allow_phase_deny():
    """Tier 2: A tool with router=allow, phase=deny appears only in for_router()."""
    registry = ToolRegistry()
    tool = _make_tool("router_only_tool", gates=ToolGates(router="allow", phase="deny"))
    registry.register(tool)

    assert tool in registry.for_router()
    assert tool not in registry.for_phase()


def test_gate_partitioning_router_deny_phase_allow():
    """Tier 2: A tool with router=deny, phase=allow appears only in for_phase()."""
    registry = ToolRegistry()
    tool = _make_tool("phase_only_tool", gates=ToolGates(router="deny", phase="allow"))
    registry.register(tool)

    assert tool not in registry.for_router()
    assert tool in registry.for_phase()


def test_gate_partitioning_both_allow():
    """Tier 2: A tool with router=allow, phase=allow appears in both filtered lists."""
    registry = ToolRegistry()
    tool = _make_tool("both_tool", gates=ToolGates(router="allow", phase="allow"))
    registry.register(tool)

    assert tool in registry.for_router()
    assert tool in registry.for_phase()


def test_gate_partitioning_both_deny():
    """Tier 2: A tool with router=deny, phase=deny appears in neither filtered list."""
    registry = ToolRegistry()
    tool = _make_tool("neither_tool", gates=ToolGates(router="deny", phase="deny"))
    registry.register(tool)

    assert tool not in registry.for_router()
    assert tool not in registry.for_phase()


# ── 13. RouterCallerState typed sub-object invariants (M4 Phase 2) ───────────

def test_router_caller_state_defaults_all_none():
    """Tier 2: RouterCallerState() with no arguments defaults all fields to None."""
    state = RouterCallerState()
    assert state.skill_registry is None
    assert state.agent_registry is None
    assert state.available_skills is None
    assert state.available_agents is None
    assert state.send_to_agent is None
    assert state.dispatch_plan_tool is None
    assert state.chain_id is None
    assert state.budget is None
    assert state.router_model is None
    assert state.available_tool_names is None
    assert state.memory_service is None


def test_phase_caller_state_defaults_all_none():
    """Tier 2: PhaseCallerState() with no arguments defaults all fields to None."""
    state = PhaseCallerState()
    assert state.skill_run_id is None
    assert state.phase_name is None
    assert state.run_visit_count is None
    assert state.op_context is None
    assert state.workspace_callbacks is None


def test_tool_context_with_router_caller_state():
    """Tier 2: ToolContext with caller_kind='router' can hold a RouterCallerState."""

    class _SentinelEvents:
        pass

    class _SentinelWorkspace:
        pass

    state = RouterCallerState(chain_id="chain-123", router_model="gpt-4o")
    ctx = ToolContext(
        events=_SentinelEvents(),
        permission_resolver=None,
        workspace=_SentinelWorkspace(),
        caller_kind="router",
        router_state=state,
    )
    assert ctx.caller_kind == "router"
    assert ctx.router_state is state
    assert ctx.router_state.chain_id == "chain-123"
    assert ctx.router_state.router_model == "gpt-4o"
    assert ctx.phase_state is None


def test_tool_context_with_phase_caller_state():
    """Tier 2: ToolContext with caller_kind='phase' can hold a PhaseCallerState."""

    class _SentinelEvents:
        pass

    class _SentinelWorkspace:
        pass

    state = PhaseCallerState(
        skill_run_id="run-abc",
        phase_name="analyze",
        run_visit_count=3,
    )
    ctx = ToolContext(
        events=_SentinelEvents(),
        permission_resolver=None,
        workspace=_SentinelWorkspace(),
        caller_kind="phase",
        phase_state=state,
    )
    assert ctx.caller_kind == "phase"
    assert ctx.phase_state is state
    assert ctx.phase_state.skill_run_id == "run-abc"
    assert ctx.phase_state.phase_name == "analyze"
    assert ctx.phase_state.run_visit_count == 3
    assert ctx.router_state is None


def test_tool_context_fields_attribute_access():
    """Tier 2: ToolContext fields are accessible via attribute access (dataclass round-trip)."""

    class _SentinelEvents:
        pass

    class _SentinelWorkspace:
        pass

    events = _SentinelEvents()
    workspace = _SentinelWorkspace()
    ctx = ToolContext(
        events=events,
        permission_resolver=None,
        workspace=workspace,
        caller_kind="router",
    )
    assert ctx.events is events
    assert ctx.workspace is workspace
    assert ctx.permission_resolver is None
    assert ctx.caller_kind == "router"
    assert ctx.router_state is None
    assert ctx.phase_state is None


def test_router_caller_state_partial_population():
    """Tier 2: RouterCallerState constructed with skill_registry only leaves
    all other fields None (= partial population for gradual migration)."""
    sentinel_registry = object()
    state = RouterCallerState(skill_registry=sentinel_registry)

    assert state.skill_registry is sentinel_registry
    assert state.agent_registry is None
    assert state.available_skills is None
    assert state.available_agents is None
    assert state.send_to_agent is None
    assert state.dispatch_plan_tool is None
    assert state.chain_id is None
    assert state.budget is None
    assert state.router_model is None
    assert state.available_tool_names is None
    assert state.memory_service is None


def test_router_caller_state_full_population():
    """Tier 2: RouterCallerState constructed with all fields preserves them."""
    sentinel_skill_reg = object()
    sentinel_agent_reg = object()
    sentinel_budget = object()
    sentinel_memory = object()

    async def _send_to_agent(*a, **kw):
        pass

    async def _dispatch_plan(*a, **kw):
        pass

    state = RouterCallerState(
        skill_registry=sentinel_skill_reg,
        agent_registry=sentinel_agent_reg,
        available_skills=[{"name": "s1"}],
        available_agents=[{"name": "a1"}],
        send_to_agent=_send_to_agent,
        dispatch_plan_tool=_dispatch_plan,
        chain_id="chain-xyz",
        budget=sentinel_budget,
        router_model="openai/gpt-4o",
        available_tool_names=["web_search", "plan"],
        memory_service=sentinel_memory,
    )

    assert state.skill_registry is sentinel_skill_reg
    assert state.agent_registry is sentinel_agent_reg
    assert state.available_skills == [{"name": "s1"}]
    assert state.available_agents == [{"name": "a1"}]
    assert state.send_to_agent is _send_to_agent
    assert state.dispatch_plan_tool is _dispatch_plan
    assert state.chain_id == "chain-xyz"
    assert state.budget is sentinel_budget
    assert state.router_model == "openai/gpt-4o"
    assert state.available_tool_names == ["web_search", "plan"]
    assert state.memory_service is sentinel_memory


# ── 14. RouterCallerState catalog callable fields (M4 Phase 3) ───────────────

def test_router_caller_state_catalog_callable_fields_default_none():
    """Tier 2: RouterCallerState catalog callable fields default to None."""
    state = RouterCallerState()
    assert state.list_skills_fn is None
    assert state.describe_skill_fn is None
    assert state.list_agents_fn is None
    assert state.describe_agent_fn is None


def test_router_caller_state_catalog_callable_fields_assignable():
    """Tier 2: RouterCallerState catalog callable fields accept callables and remain callable."""
    def _list_skills(query: str) -> list:
        return [{"name": "s1", "query": query}]

    def _describe_skill(name: str) -> dict:
        return {"name": name, "description": "desc"}

    def _list_agents(query: str) -> list:
        return [{"name": "a1", "query": query}]

    def _describe_agent(name: str) -> dict:
        return {"name": name, "description": "agent desc"}

    state = RouterCallerState(
        list_skills_fn=_list_skills,
        describe_skill_fn=_describe_skill,
        list_agents_fn=_list_agents,
        describe_agent_fn=_describe_agent,
    )

    assert callable(state.list_skills_fn)
    assert callable(state.describe_skill_fn)
    assert callable(state.list_agents_fn)
    assert callable(state.describe_agent_fn)

    skills = state.list_skills_fn("test_query")
    assert isinstance(skills, list)
    assert skills[0]["name"] == "s1"

    skill = state.describe_skill_fn("my_skill")
    assert isinstance(skill, dict)
    assert skill["name"] == "my_skill"

    agents = state.list_agents_fn("agent_query")
    assert isinstance(agents, list)
    assert agents[0]["name"] == "a1"

    agent = state.describe_agent_fn("my_agent")
    assert isinstance(agent, dict)
    assert agent["name"] == "my_agent"


# ── 15. ToolDefinition schema_enricher field (M4 Phase 3) ────────────────────

def test_tool_definition_schema_enricher_defaults_none():
    """Tier 2: ToolDefinition.schema_enricher defaults to None when not specified."""
    tool = _make_tool("enricher_default_tool")
    assert tool.schema_enricher is None


def test_tool_definition_schema_enricher_can_be_set():
    """Tier 2: ToolDefinition.schema_enricher accepts a callable and can be invoked."""
    def _enricher(rendered: dict, state: RouterCallerState) -> dict:
        enriched = dict(rendered)
        enriched["_enriched"] = True
        enriched["_skills_count"] = len(state.available_skills or [])
        return enriched

    tool = ToolDefinition(
        name="enricher_tool",
        description="Tool with schema enricher.",
        parameters={"type": "object", "properties": {}, "required": []},
        gates=ToolGates(),
        handler=_noop_handler(),
        category="discovery",
        schema_enricher=_enricher,
    )

    assert callable(tool.schema_enricher)

    sample_rendered = {"type": "function", "function": {"name": "enricher_tool"}}
    state = RouterCallerState(available_skills=[{"name": "skill_a"}, {"name": "skill_b"}])
    result = tool.schema_enricher(sample_rendered, state)

    assert result["_enriched"] is True
    assert result["_skills_count"] == 2
    assert result["type"] == "function"
