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
