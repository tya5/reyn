"""Tier 2: catalog ToolDefinitions ADR-0026 M3 Wave 2 invariants.

Covers 2 ToolDefinitions from src/reyn/tools/catalog.py:
  LIST_AGENTS, DESCRIBE_AGENT.

Type C closure invariants (phase="allow"):
  Both tools have gates.router="allow" AND gates.phase="allow",
  enabling future phase-side catalog browse dispatch in M4.

Each ToolDefinition is verified for:
  - render_for_router() byte-identity (description + parameters match
    router_tools.py ToolSpec literals — LLMReplay fixture safety).
  - gates.router="allow", gates.phase="allow" (Type C closure).
  - purity="read_only", category="discovery".
  - ToolRegistry registration (for_router and for_phase both include).

No mocks of collaborators. All tests use real ToolDefinition /
ToolRegistry instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.catalog import (
    _DESCRIBE_AGENT_DESCRIPTION,
    _DESCRIBE_AGENT_PARAMETERS,
    _LIST_AGENTS_DESCRIPTION,
    _LIST_AGENTS_PARAMETERS,
    DESCRIBE_AGENT,
    LIST_AGENTS,
)
from reyn.tools.registry import ToolRegistry

# ── Shared fixture ────────────────────────────────────────────────────────────

def _make_registry() -> ToolRegistry:
    """Build a ToolRegistry with the catalog ToolDefinitions registered."""
    r = ToolRegistry()
    r.register(LIST_AGENTS)
    r.register(DESCRIBE_AGENT)
    return r


def _minimal_ctx():
    """Minimal ToolContext for handler invocation tests."""
    from reyn.tools.types import ToolContext
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. list_agents — render_for_router byte-identity
# ══════════════════════════════════════════════════════════════════════════════

def test_list_agents_render_exact_description():
    """Tier 2: LIST_AGENTS description is byte-identical to the
    router_tools.py ToolSpec literal."""
    rendered = LIST_AGENTS.render_for_router()
    legacy = (
        "Browse peer agents reachable via topology. "
        "Pass empty path for clusters; "
        "pass a cluster name for agents in it."
    )
    assert rendered["function"]["description"] == legacy


def test_list_agents_render_exact_parameters():
    """Tier 2: LIST_AGENTS parameters schema is byte-identical to the
    router_tools.py ToolSpec parameters for list_agents."""
    rendered = LIST_AGENTS.render_for_router()
    legacy = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy


def test_list_agents_constants_match_render():
    """Tier 2: _LIST_AGENTS_DESCRIPTION / _LIST_AGENTS_PARAMETERS match
    what LIST_AGENTS.description / .parameters hold."""
    rendered = LIST_AGENTS.render_for_router()
    assert rendered["function"]["description"] == _LIST_AGENTS_DESCRIPTION
    assert LIST_AGENTS.description == _LIST_AGENTS_DESCRIPTION
    assert rendered["function"]["parameters"] == _LIST_AGENTS_PARAMETERS
    assert dict(LIST_AGENTS.parameters) == _LIST_AGENTS_PARAMETERS


def test_list_agents_gates_type_c():
    """Tier 2: LIST_AGENTS has gates.router=allow AND gates.phase=allow
    (Type C closure)."""
    assert LIST_AGENTS.gates.router == "allow"
    assert LIST_AGENTS.gates.phase == "allow"


def test_list_agents_purity_and_category():
    """Tier 2: LIST_AGENTS purity=read_only, category=discovery."""
    assert LIST_AGENTS.purity == "read_only"
    assert LIST_AGENTS.category == "discovery"


# ══════════════════════════════════════════════════════════════════════════════
# 4. describe_agent — render_for_router byte-identity
# ══════════════════════════════════════════════════════════════════════════════

def test_describe_agent_render_exact_description():
    """Tier 2: DESCRIBE_AGENT description is byte-identical to the
    router_tools.py ToolSpec literal."""
    rendered = DESCRIBE_AGENT.render_for_router()
    legacy = (
        "Fetch full role / capabilities profile for one agent. "
        "Call before delegate_to_agent if uncertain."
    )
    assert rendered["function"]["description"] == legacy


def test_describe_agent_render_exact_parameters():
    """Tier 2: DESCRIBE_AGENT parameters schema is byte-identical to the
    router_tools.py ToolSpec parameters for describe_agent."""
    rendered = DESCRIBE_AGENT.render_for_router()
    legacy = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
        },
        "required": ["name"],
    }
    assert rendered["function"]["parameters"] == legacy


def test_describe_agent_constants_match_render():
    """Tier 2: _DESCRIBE_AGENT_DESCRIPTION / _DESCRIBE_AGENT_PARAMETERS match
    what DESCRIBE_AGENT.description / .parameters hold."""
    rendered = DESCRIBE_AGENT.render_for_router()
    assert rendered["function"]["description"] == _DESCRIBE_AGENT_DESCRIPTION
    assert DESCRIBE_AGENT.description == _DESCRIBE_AGENT_DESCRIPTION
    assert rendered["function"]["parameters"] == _DESCRIBE_AGENT_PARAMETERS
    assert dict(DESCRIBE_AGENT.parameters) == _DESCRIBE_AGENT_PARAMETERS


def test_describe_agent_gates_type_c():
    """Tier 2: DESCRIBE_AGENT has gates.router=allow AND gates.phase=allow
    (Type C closure)."""
    assert DESCRIBE_AGENT.gates.router == "allow"
    assert DESCRIBE_AGENT.gates.phase == "allow"


def test_describe_agent_purity_and_category():
    """Tier 2: DESCRIBE_AGENT purity=read_only, category=discovery."""
    assert DESCRIBE_AGENT.purity == "read_only"
    assert DESCRIBE_AGENT.category == "discovery"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Registry invariants — catalog tools in for_router() AND for_phase()
# ══════════════════════════════════════════════════════════════════════════════

def test_registry_contains_catalog_tools():
    """Tier 2: A ToolRegistry with the catalog tools registered contains
    their names."""
    registry = _make_registry()
    for name in ("list_agents", "describe_agent"):
        assert name in registry


def test_registry_catalog_tools_in_for_router():
    """Tier 2: The catalog tools appear in registry.for_router()
    (gates.router=allow on all)."""
    registry = _make_registry()
    router_tools = registry.for_router()
    assert LIST_AGENTS in router_tools
    assert DESCRIBE_AGENT in router_tools


def test_registry_catalog_tools_in_for_phase():
    """Tier 2: The catalog tools appear in registry.for_phase()
    (gates.phase=allow on all — Type C closure)."""
    registry = _make_registry()
    phase_tools = registry.for_phase()
    assert LIST_AGENTS in phase_tools
    assert DESCRIBE_AGENT in phase_tools


def test_registry_lookup_returns_correct_instances():
    """Tier 2: registry.lookup() returns the correct singleton instance for
    each catalog tool."""
    registry = _make_registry()
    assert registry.lookup("list_agents") is LIST_AGENTS
    assert registry.lookup("describe_agent") is DESCRIBE_AGENT


# ══════════════════════════════════════════════════════════════════════════════
# 4. Handler activated (M4 Phase 3) — require router_state.<fn>
#    Activation tests with happy-path delegation live in
#    tests/test_tool_registry_handlers.py.  These tests pin the
#    mis-wiring contract: missing router_state → RuntimeError.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_agents_handler_raises_when_router_state_missing():
    """Tier 2: LIST_AGENTS.handler raises RuntimeError on missing
    router_state (= M4 Phase 3 activation contract)."""
    ctx = _minimal_ctx()
    with pytest.raises(RuntimeError, match="list_agents_fn"):
        await LIST_AGENTS.handler({"path": ""}, ctx)


@pytest.mark.asyncio
async def test_describe_agent_handler_raises_when_router_state_missing():
    """Tier 2: DESCRIBE_AGENT.handler raises RuntimeError on missing
    router_state (= M4 Phase 3 activation contract)."""
    ctx = _minimal_ctx()
    with pytest.raises(RuntimeError, match="describe_agent_fn"):
        await DESCRIBE_AGENT.handler({"name": "some_agent"}, ctx)
