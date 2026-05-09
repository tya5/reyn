"""Tier 2: catalog ToolDefinitions ADR-0026 M3 Wave 2 invariants.

Covers 4 ToolDefinitions from src/reyn/tools/catalog.py:
  LIST_SKILLS, DESCRIBE_SKILL, LIST_AGENTS, DESCRIBE_AGENT.

Type C closure invariants (phase="allow"):
  All 4 tools have gates.router="allow" AND gates.phase="allow",
  enabling future phase-side catalog browse dispatch in M4.

Each ToolDefinition is verified for:
  - render_for_router() byte-identity (description + parameters match
    router_tools.py ToolSpec literals — LLMReplay fixture safety).
  - gates.router="allow", gates.phase="allow" (Type C closure).
  - purity="read_only", category="discovery".
  - ToolRegistry registration (for_router and for_phase both include).
  - Handler raises NotImplementedError (design-revisit stub; see
    catalog.py module docstring for M4 rationale).

No dynamic enum injection applies to these tools (unlike invoke_skill /
delegate_to_agent). The parameters schemas are fully static.

No mocks of collaborators. All tests use real ToolDefinition /
ToolRegistry instances. No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.registry import ToolRegistry
from reyn.tools.catalog import (
    LIST_SKILLS,
    DESCRIBE_SKILL,
    LIST_AGENTS,
    DESCRIBE_AGENT,
    _LIST_SKILLS_DESCRIPTION,
    _LIST_SKILLS_PARAMETERS,
    _DESCRIBE_SKILL_DESCRIPTION,
    _DESCRIBE_SKILL_PARAMETERS,
    _LIST_AGENTS_DESCRIPTION,
    _LIST_AGENTS_PARAMETERS,
    _DESCRIBE_AGENT_DESCRIPTION,
    _DESCRIBE_AGENT_PARAMETERS,
)


# ── Shared fixture ────────────────────────────────────────────────────────────

def _make_registry() -> ToolRegistry:
    """Build a ToolRegistry with all 4 catalog ToolDefinitions registered."""
    r = ToolRegistry()
    r.register(LIST_SKILLS)
    r.register(DESCRIBE_SKILL)
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
# 1. list_skills — render_for_router byte-identity
# ══════════════════════════════════════════════════════════════════════════════

def test_list_skills_render_shape():
    """Tier 2: LIST_SKILLS.render_for_router() has the correct top-level shape
    expected by the LiteLLM tools[] argument."""
    rendered = LIST_SKILLS.render_for_router()
    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "list_skills"
    assert isinstance(fn["description"], str)
    assert isinstance(fn["parameters"], dict)


def test_list_skills_render_exact_description():
    """Tier 2: LIST_SKILLS description is byte-identical to the router_tools.py
    ToolSpec literal. Any character diff invalidates LLMReplay fixtures."""
    rendered = LIST_SKILLS.render_for_router()
    legacy = (
        "Browse the skill catalogue hierarchically. "
        "Pass empty string to see top-level categories. "
        "Pass a category path to drill in. "
        "Returns either child categories or items, "
        "each with name and one-line description. "
        "After this returns, narrate the skill names directly to "
        "the user in your next message — do not stop after listing "
        "and do not ask for confirmation before naming them."
    )
    assert rendered["function"]["description"] == legacy


def test_list_skills_render_exact_parameters():
    """Tier 2: LIST_SKILLS parameters schema is byte-identical to the
    router_tools.py ToolSpec parameters for list_skills."""
    rendered = LIST_SKILLS.render_for_router()
    legacy = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    'Category path, e.g. "", "write", "write/blog". '
                    "Empty = root."
                ),
            }
        },
        "required": ["path"],
    }
    assert rendered["function"]["parameters"] == legacy


def test_list_skills_constants_match_render():
    """Tier 2: _LIST_SKILLS_DESCRIPTION and _LIST_SKILLS_PARAMETERS module
    constants match what LIST_SKILLS.description / .parameters hold."""
    rendered = LIST_SKILLS.render_for_router()
    assert rendered["function"]["description"] == _LIST_SKILLS_DESCRIPTION
    assert LIST_SKILLS.description == _LIST_SKILLS_DESCRIPTION
    assert rendered["function"]["parameters"] == _LIST_SKILLS_PARAMETERS
    assert dict(LIST_SKILLS.parameters) == _LIST_SKILLS_PARAMETERS


# ── Type C gate ──────────────────────────────────────────────────────────────

def test_list_skills_gates_type_c():
    """Tier 2: LIST_SKILLS has gates.router=allow AND gates.phase=allow (Type C
    closure — catalog browse enabled for both router and phase in M4)."""
    assert LIST_SKILLS.gates.router == "allow"
    assert LIST_SKILLS.gates.phase == "allow"


def test_list_skills_purity_and_category():
    """Tier 2: LIST_SKILLS purity=read_only, category=discovery."""
    assert LIST_SKILLS.purity == "read_only"
    assert LIST_SKILLS.category == "discovery"


# ══════════════════════════════════════════════════════════════════════════════
# 2. describe_skill — render_for_router byte-identity
# ══════════════════════════════════════════════════════════════════════════════

def test_describe_skill_render_exact_description():
    """Tier 2: DESCRIBE_SKILL description is byte-identical to the
    router_tools.py ToolSpec literal."""
    rendered = DESCRIBE_SKILL.render_for_router()
    legacy = (
        "Fetch full metadata for one skill: when_to_use, examples, "
        "input artifact schema. "
        "Call this before invoke_skill if you're unsure how to "
        "construct the input."
    )
    assert rendered["function"]["description"] == legacy


def test_describe_skill_render_exact_parameters():
    """Tier 2: DESCRIBE_SKILL parameters schema is byte-identical to the
    router_tools.py ToolSpec parameters for describe_skill."""
    rendered = DESCRIBE_SKILL.render_for_router()
    legacy = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
        },
        "required": ["name"],
    }
    assert rendered["function"]["parameters"] == legacy


def test_describe_skill_constants_match_render():
    """Tier 2: _DESCRIBE_SKILL_DESCRIPTION / _DESCRIBE_SKILL_PARAMETERS match
    what DESCRIBE_SKILL.description / .parameters hold."""
    rendered = DESCRIBE_SKILL.render_for_router()
    assert rendered["function"]["description"] == _DESCRIBE_SKILL_DESCRIPTION
    assert DESCRIBE_SKILL.description == _DESCRIBE_SKILL_DESCRIPTION
    assert rendered["function"]["parameters"] == _DESCRIBE_SKILL_PARAMETERS
    assert dict(DESCRIBE_SKILL.parameters) == _DESCRIBE_SKILL_PARAMETERS


def test_describe_skill_gates_type_c():
    """Tier 2: DESCRIBE_SKILL has gates.router=allow AND gates.phase=allow
    (Type C closure)."""
    assert DESCRIBE_SKILL.gates.router == "allow"
    assert DESCRIBE_SKILL.gates.phase == "allow"


def test_describe_skill_purity_and_category():
    """Tier 2: DESCRIBE_SKILL purity=read_only, category=discovery."""
    assert DESCRIBE_SKILL.purity == "read_only"
    assert DESCRIBE_SKILL.category == "discovery"


# ══════════════════════════════════════════════════════════════════════════════
# 3. list_agents — render_for_router byte-identity
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
# 5. Registry invariants — all 4 tools in for_router() AND for_phase()
# ══════════════════════════════════════════════════════════════════════════════

def test_registry_contains_all_four():
    """Tier 2: A ToolRegistry with all 4 catalog tools registered contains
    all 4 names."""
    registry = _make_registry()
    for name in ("list_skills", "describe_skill", "list_agents", "describe_agent"):
        assert name in registry


def test_registry_all_four_in_for_router():
    """Tier 2: All 4 catalog tools appear in registry.for_router()
    (gates.router=allow on all)."""
    registry = _make_registry()
    router_tools = registry.for_router()
    assert LIST_SKILLS in router_tools
    assert DESCRIBE_SKILL in router_tools
    assert LIST_AGENTS in router_tools
    assert DESCRIBE_AGENT in router_tools


def test_registry_all_four_in_for_phase():
    """Tier 2: All 4 catalog tools appear in registry.for_phase()
    (gates.phase=allow on all — Type C closure)."""
    registry = _make_registry()
    phase_tools = registry.for_phase()
    assert LIST_SKILLS in phase_tools
    assert DESCRIBE_SKILL in phase_tools
    assert LIST_AGENTS in phase_tools
    assert DESCRIBE_AGENT in phase_tools


def test_registry_lookup_returns_correct_instances():
    """Tier 2: registry.lookup() returns the correct singleton instance for
    each of the 4 catalog tools."""
    registry = _make_registry()
    assert registry.lookup("list_skills") is LIST_SKILLS
    assert registry.lookup("describe_skill") is DESCRIBE_SKILL
    assert registry.lookup("list_agents") is LIST_AGENTS
    assert registry.lookup("describe_agent") is DESCRIBE_AGENT


# ══════════════════════════════════════════════════════════════════════════════
# 6. Handler activated (M4 Phase 3) — all 4 require router_state.<fn>
#    Activation tests with happy-path delegation live in
#    tests/test_tool_registry_handlers.py.  These tests pin the
#    mis-wiring contract: missing router_state → RuntimeError.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_skills_handler_raises_when_router_state_missing():
    """Tier 2: LIST_SKILLS.handler raises RuntimeError when ctx.router_state
    is None (= mis-wired dispatcher; M4 Phase 3 activation contract)."""
    ctx = _minimal_ctx()
    with pytest.raises(RuntimeError, match="list_skills_fn"):
        await LIST_SKILLS.handler({"path": ""}, ctx)


@pytest.mark.asyncio
async def test_describe_skill_handler_raises_when_router_state_missing():
    """Tier 2: DESCRIBE_SKILL.handler raises RuntimeError on missing
    router_state (= M4 Phase 3 activation contract)."""
    ctx = _minimal_ctx()
    with pytest.raises(RuntimeError, match="describe_skill_fn"):
        await DESCRIBE_SKILL.handler({"name": "some_skill"}, ctx)


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
