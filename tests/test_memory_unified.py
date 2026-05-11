"""Tier 2 invariants for the unified memory ToolDefinitions (ADR-0026 M3 Wave 2).

Covers:
  - Render shape for all 5 ToolDefinitions (render_for_router / render_for_phase)
  - Type C closure: all 5 have gates.phase="allow"
  - Purity classification per definition
  - Category classification per definition
  - Per-call schema dynamics (layer enum on read_memory_body / forget_memory;
    type enum on remember_shared / remember_agent)

No unittest.mock / MagicMock / AsyncMock. All tests use real ToolDefinition
instances and workspace fixtures where async execution is needed.
"""
from __future__ import annotations

import pytest

from reyn.tools.memory import (
    FORGET_MEMORY,
    LIST_MEMORY,
    READ_MEMORY_BODY,
    REMEMBER_AGENT,
    REMEMBER_SHARED,
)
from reyn.tools.types import ToolDefinition, ToolGates

# ── 1. All 5 are ToolDefinition instances ────────────────────────────────────────

def test_list_memory_is_tool_definition():
    """Tier 2: LIST_MEMORY is a ToolDefinition instance."""
    assert isinstance(LIST_MEMORY, ToolDefinition)


def test_read_memory_body_is_tool_definition():
    """Tier 2: READ_MEMORY_BODY is a ToolDefinition instance."""
    assert isinstance(READ_MEMORY_BODY, ToolDefinition)


def test_remember_shared_is_tool_definition():
    """Tier 2: REMEMBER_SHARED is a ToolDefinition instance."""
    assert isinstance(REMEMBER_SHARED, ToolDefinition)


def test_remember_agent_is_tool_definition():
    """Tier 2: REMEMBER_AGENT is a ToolDefinition instance."""
    assert isinstance(REMEMBER_AGENT, ToolDefinition)


def test_forget_memory_is_tool_definition():
    """Tier 2: FORGET_MEMORY is a ToolDefinition instance."""
    assert isinstance(FORGET_MEMORY, ToolDefinition)


# ── 2. Canonical names ───────────────────────────────────────────────────────────

def test_canonical_names():
    """Tier 2: each ToolDefinition carries the expected canonical name."""
    assert LIST_MEMORY.name == "list_memory"
    assert READ_MEMORY_BODY.name == "read_memory_body"
    assert REMEMBER_SHARED.name == "remember_shared"
    assert REMEMBER_AGENT.name == "remember_agent"
    assert FORGET_MEMORY.name == "forget_memory"


# ── 3. Type C closure — gates.phase="allow" for all 5 ──────────────────────────

def test_type_c_closure_list_memory():
    """Tier 2: LIST_MEMORY has gates.phase='allow' (Type C closure)."""
    assert LIST_MEMORY.gates.phase == "allow"


def test_type_c_closure_read_memory_body():
    """Tier 2: READ_MEMORY_BODY has gates.phase='allow' (Type C closure)."""
    assert READ_MEMORY_BODY.gates.phase == "allow"


def test_type_c_closure_remember_shared():
    """Tier 2: REMEMBER_SHARED has gates.phase='allow' (Type C closure)."""
    assert REMEMBER_SHARED.gates.phase == "allow"


def test_type_c_closure_remember_agent():
    """Tier 2: REMEMBER_AGENT has gates.phase='allow' (Type C closure)."""
    assert REMEMBER_AGENT.gates.phase == "allow"


def test_type_c_closure_forget_memory():
    """Tier 2: FORGET_MEMORY has gates.phase='allow' (Type C closure)."""
    assert FORGET_MEMORY.gates.phase == "allow"


# ── 4. All 5 also have gates.router="allow" ──────────────────────────────────────

def test_router_allow_all_five():
    """Tier 2: all 5 memory ToolDefinitions have gates.router='allow'."""
    for defn in (LIST_MEMORY, READ_MEMORY_BODY, REMEMBER_SHARED, REMEMBER_AGENT, FORGET_MEMORY):
        assert defn.gates.router == "allow", f"{defn.name}: expected router=allow"


# ── 5. Purity classification ──────────────────────────────────────────────────────

def test_purity_read_only_tools():
    """Tier 2: LIST_MEMORY and READ_MEMORY_BODY have purity='read_only'."""
    assert LIST_MEMORY.purity == "read_only"
    assert READ_MEMORY_BODY.purity == "read_only"


def test_purity_side_effect_tools():
    """Tier 2: REMEMBER_SHARED, REMEMBER_AGENT, FORGET_MEMORY have purity='side_effect'."""
    assert REMEMBER_SHARED.purity == "side_effect"
    assert REMEMBER_AGENT.purity == "side_effect"
    assert FORGET_MEMORY.purity == "side_effect"


# ── 6. Category classification ───────────────────────────────────────────────────

def test_category_all_memory():
    """Tier 2: all 5 memory ToolDefinitions have category='memory'."""
    for defn in (LIST_MEMORY, READ_MEMORY_BODY, REMEMBER_SHARED, REMEMBER_AGENT, FORGET_MEMORY):
        assert defn.category == "memory", f"{defn.name}: expected category='memory'"


# ── 7. render_for_router shape ───────────────────────────────────────────────────

def test_render_for_router_list_memory():
    """Tier 2: LIST_MEMORY.render_for_router() produces valid OpenAI tools[] shape."""
    rendered = LIST_MEMORY.render_for_router()
    assert rendered["type"] == "function"
    fn = rendered["function"]
    assert fn["name"] == "list_memory"
    assert "path" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["path"]


def test_render_for_router_read_memory_body():
    """Tier 2: READ_MEMORY_BODY.render_for_router() includes layer enum + slug."""
    rendered = READ_MEMORY_BODY.render_for_router()
    fn = rendered["function"]
    assert fn["name"] == "read_memory_body"
    props = fn["parameters"]["properties"]
    assert "layer" in props
    assert props["layer"].get("enum") == ["shared", "agent"]
    assert "slug" in props
    assert set(fn["parameters"]["required"]) == {"layer", "slug"}


def test_render_for_router_remember_shared():
    """Tier 2: REMEMBER_SHARED.render_for_router() includes type enum + required fields."""
    rendered = REMEMBER_SHARED.render_for_router()
    fn = rendered["function"]
    assert fn["name"] == "remember_shared"
    props = fn["parameters"]["properties"]
    assert props["type"].get("enum") == ["user", "feedback", "project", "reference"]
    assert set(fn["parameters"]["required"]) == {"slug", "name", "description", "type", "body"}


def test_render_for_router_remember_agent():
    """Tier 2: REMEMBER_AGENT.render_for_router() schema mirrors remember_shared schema."""
    rendered = REMEMBER_AGENT.render_for_router()
    fn = rendered["function"]
    assert fn["name"] == "remember_agent"
    props = fn["parameters"]["properties"]
    assert props["type"].get("enum") == ["user", "feedback", "project", "reference"]
    assert set(fn["parameters"]["required"]) == {"slug", "name", "description", "type", "body"}


def test_render_for_router_forget_memory():
    """Tier 2: FORGET_MEMORY.render_for_router() includes layer enum + slug required."""
    rendered = FORGET_MEMORY.render_for_router()
    fn = rendered["function"]
    assert fn["name"] == "forget_memory"
    props = fn["parameters"]["properties"]
    assert props["layer"].get("enum") == ["shared", "agent"]
    assert set(fn["parameters"]["required"]) == {"layer", "slug"}


# ── 8. render_for_phase shape ────────────────────────────────────────────────────

def test_render_for_phase_list_memory():
    """Tier 2: LIST_MEMORY.render_for_phase() has kind, description, args_schema, purity."""
    rendered = LIST_MEMORY.render_for_phase()
    assert rendered["kind"] == "list_memory"
    assert rendered["purity"] == "read_only"
    assert "path" in rendered["args_schema"]["properties"]


def test_render_for_phase_remember_shared():
    """Tier 2: REMEMBER_SHARED.render_for_phase() purity='side_effect'."""
    rendered = REMEMBER_SHARED.render_for_phase()
    assert rendered["kind"] == "remember_shared"
    assert rendered["purity"] == "side_effect"


def test_render_for_phase_forget_memory():
    """Tier 2: FORGET_MEMORY.render_for_phase() purity='side_effect'."""
    rendered = FORGET_MEMORY.render_for_phase()
    assert rendered["kind"] == "forget_memory"
    assert rendered["purity"] == "side_effect"


# ── 9. Schema dynamics — layer enum present on write+delete tools ─────────────────

def test_layer_enum_in_read_memory_body_schema():
    """Tier 2: read_memory_body parameters include a 'layer' enum with shared/agent."""
    schema = READ_MEMORY_BODY.parameters
    assert schema["properties"]["layer"]["enum"] == ["shared", "agent"]


def test_layer_enum_in_forget_memory_schema():
    """Tier 2: forget_memory parameters include a 'layer' enum with shared/agent."""
    schema = FORGET_MEMORY.parameters
    assert schema["properties"]["layer"]["enum"] == ["shared", "agent"]


def test_type_enum_in_remember_shared_schema():
    """Tier 2: remember_shared type enum contains the 4 canonical memory types."""
    enum = REMEMBER_SHARED.parameters["properties"]["type"]["enum"]
    assert set(enum) == {"user", "feedback", "project", "reference"}


def test_type_enum_in_remember_agent_schema():
    """Tier 2: remember_agent type enum contains the 4 canonical memory types."""
    enum = REMEMBER_AGENT.parameters["properties"]["type"]["enum"]
    assert set(enum) == {"user", "feedback", "project", "reference"}


# ── 10. ToolDefinitions are frozen (mutation raises) ─────────────────────────────

def test_tool_definitions_are_frozen():
    """Tier 2: all 5 memory ToolDefinitions are frozen; mutation raises FrozenInstanceError."""
    import dataclasses
    for defn in (LIST_MEMORY, READ_MEMORY_BODY, REMEMBER_SHARED, REMEMBER_AGENT, FORGET_MEMORY):
        with pytest.raises(dataclasses.FrozenInstanceError):
            defn.name = "mutated"  # type: ignore[misc]


# ── 11. render_for_router parameters_is_dict ─────────────────────────────────────

def test_render_for_router_parameters_are_plain_dicts():
    """Tier 2: render_for_router() parameters field is a plain dict for all 5."""
    for defn in (LIST_MEMORY, READ_MEMORY_BODY, REMEMBER_SHARED, REMEMBER_AGENT, FORGET_MEMORY):
        rendered = defn.render_for_router()
        assert type(rendered["function"]["parameters"]) is dict, (
            f"{defn.name}: parameters should be plain dict"
        )
