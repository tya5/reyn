"""Tier 2: MCP ToolDefinitions M3 Wave 2 invariants (ADR-0026 M3 + Type C closure).

Verifies that CALL_MCP_TOOL, LIST_MCP_SERVERS, and LIST_MCP_TOOLS ToolDefinitions:
- Produce correct description/parameters output matching the FP-0032 contract.
  (Prior byte-identity tests against the legacy ``tool`` param name have been
  updated to reflect the FP-0032 vocabulary unification: ``tool`` → ``mcp_tool_name``.)
- Have the correct gates (all 3 have router=allow, phase=allow — Type C closure).
- Have correct purity and category.
- Are renderable via render_for_router() and render_for_phase().
- Guard against polymorphic args contract for call_mcp_tool.

No mocks of collaborators. All tests use real ToolDefinition instances.
No private state assertions.
"""
from __future__ import annotations

import pytest

from reyn.tools.mcp import (
    _CALL_MCP_TOOL_DESCRIPTION,
    _CALL_MCP_TOOL_PARAMETERS,
    _LIST_MCP_SERVERS_DESCRIPTION,
    _LIST_MCP_SERVERS_PARAMETERS,
    _LIST_MCP_TOOLS_DESCRIPTION,
    _LIST_MCP_TOOLS_PARAMETERS,
    CALL_MCP_TOOL,
    LIST_MCP_SERVERS,
    LIST_MCP_TOOLS,
)
from reyn.tools.registry import ToolRegistry

# ── 1. CALL_MCP_TOOL — byte-identity gate ─────────────────────────────────────

def test_call_mcp_tool_router_render_exact_description():
    """Tier 2: CALL_MCP_TOOL description matches the FP-0032 vocabulary contract
    (mcp_tool_name instead of tool, describe_mcp_tool instead of list_mcp_tools)."""
    rendered = CALL_MCP_TOOL.render_for_router()
    fp0032_description = (
        "Invoke a mcp_tool on an MCP server. Construct args matching "
        "the mcp_tool's input schema (see describe_mcp_tool)."
    )
    assert rendered["function"]["description"] == fp0032_description


def test_call_mcp_tool_router_render_exact_parameters():
    """Tier 2: CALL_MCP_TOOL parameters schema matches the FP-0032 contract
    (mcp_tool_name replaces tool; enum-injectable descriptions on server + mcp_tool_name)."""
    rendered = CALL_MCP_TOOL.render_for_router()
    params = rendered["function"]["parameters"]
    assert params["type"] == "object"
    assert "server" in params["properties"]
    assert "mcp_tool_name" in params["properties"]
    assert "args" in params["properties"]
    assert "tool" not in params["properties"], (
        "Legacy 'tool' param must be removed — FP-0032 renames to 'mcp_tool_name'"
    )
    assert set(params["required"]) == {"server", "mcp_tool_name", "args"}


def test_call_mcp_tool_router_render_name():
    """Tier 2: CALL_MCP_TOOL.name is 'call_mcp_tool' (ADR-0026 Open Q #6 canonical)."""
    assert CALL_MCP_TOOL.name == "call_mcp_tool"
    assert CALL_MCP_TOOL.render_for_router()["function"]["name"] == "call_mcp_tool"


def test_call_mcp_tool_constants_match_definition():
    """Tier 2: _CALL_MCP_TOOL_DESCRIPTION and _CALL_MCP_TOOL_PARAMETERS module
    constants match the CALL_MCP_TOOL ToolDefinition fields."""
    assert CALL_MCP_TOOL.description == _CALL_MCP_TOOL_DESCRIPTION
    assert dict(CALL_MCP_TOOL.parameters) == _CALL_MCP_TOOL_PARAMETERS


# ── 2. LIST_MCP_SERVERS — byte-identity gate ──────────────────────────────────

def test_list_mcp_servers_router_render_exact_description():
    """Tier 2: LIST_MCP_SERVERS description is byte-identical to the legacy ToolSpec
    description in router_tools.py D1. Any diff is a stop signal."""
    rendered = LIST_MCP_SERVERS.render_for_router()
    legacy_description = (
        "List available MCP servers configured for this agent. "
        "Returns name + description per server."
    )
    assert rendered["function"]["description"] == legacy_description


def test_list_mcp_servers_router_render_exact_parameters():
    """Tier 2: LIST_MCP_SERVERS parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py D1 (empty object, no required fields)."""
    rendered = LIST_MCP_SERVERS.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


def test_list_mcp_servers_router_render_name():
    """Tier 2: LIST_MCP_SERVERS.name is 'list_mcp_servers' (ADR-0026 Open Q #6 canonical)."""
    assert LIST_MCP_SERVERS.name == "list_mcp_servers"
    assert LIST_MCP_SERVERS.render_for_router()["function"]["name"] == "list_mcp_servers"


def test_list_mcp_servers_constants_match_definition():
    """Tier 2: _LIST_MCP_SERVERS_DESCRIPTION and _LIST_MCP_SERVERS_PARAMETERS module
    constants match the LIST_MCP_SERVERS ToolDefinition fields."""
    assert LIST_MCP_SERVERS.description == _LIST_MCP_SERVERS_DESCRIPTION
    assert dict(LIST_MCP_SERVERS.parameters) == _LIST_MCP_SERVERS_PARAMETERS


# ── 3. LIST_MCP_TOOLS — byte-identity gate ────────────────────────────────────

def test_list_mcp_tools_router_render_exact_description():
    """Tier 2: LIST_MCP_TOOLS description is byte-identical to the legacy ToolSpec
    description in router_tools.py D2. Any diff is a stop signal."""
    rendered = LIST_MCP_TOOLS.render_for_router()
    legacy_description = (
        "List tools exposed by one MCP server "
        "(with description per tool)."
    )
    assert rendered["function"]["description"] == legacy_description


def test_list_mcp_tools_router_render_exact_parameters():
    """Tier 2: LIST_MCP_TOOLS parameters schema is byte-identical to the legacy
    ToolSpec parameters in router_tools.py D2 (server string, required)."""
    rendered = LIST_MCP_TOOLS.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string"},
        },
        "required": ["server"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


def test_list_mcp_tools_router_render_name():
    """Tier 2: LIST_MCP_TOOLS.name is 'list_mcp_tools' (ADR-0026 Open Q #6 canonical)."""
    assert LIST_MCP_TOOLS.name == "list_mcp_tools"
    assert LIST_MCP_TOOLS.render_for_router()["function"]["name"] == "list_mcp_tools"


def test_list_mcp_tools_constants_match_definition():
    """Tier 2: _LIST_MCP_TOOLS_DESCRIPTION and _LIST_MCP_TOOLS_PARAMETERS module
    constants match the LIST_MCP_TOOLS ToolDefinition fields."""
    assert LIST_MCP_TOOLS.description == _LIST_MCP_TOOLS_DESCRIPTION
    assert dict(LIST_MCP_TOOLS.parameters) == _LIST_MCP_TOOLS_PARAMETERS


# ── 4. Gate invariants (Type C closure: all 3 have phase=allow) ───────────────

def test_call_mcp_tool_gates_both_allow():
    """Tier 2: CALL_MCP_TOOL has gates.router=allow and gates.phase=allow."""
    assert CALL_MCP_TOOL.gates.router == "allow"
    assert CALL_MCP_TOOL.gates.phase == "allow"


def test_list_mcp_servers_gates_both_allow():
    """Tier 2: LIST_MCP_SERVERS has gates.router=allow and gates.phase=allow.
    Type C closure: phase side now sees this capability in the registry."""
    assert LIST_MCP_SERVERS.gates.router == "allow"
    assert LIST_MCP_SERVERS.gates.phase == "allow"


def test_list_mcp_tools_gates_both_allow():
    """Tier 2: LIST_MCP_TOOLS has gates.router=allow and gates.phase=allow.
    Type C closure: phase side now sees this capability in the registry."""
    assert LIST_MCP_TOOLS.gates.router == "allow"
    assert LIST_MCP_TOOLS.gates.phase == "allow"


# ── 5. Purity and category ────────────────────────────────────────────────────

def test_call_mcp_tool_purity_side_effect():
    """Tier 2: CALL_MCP_TOOL purity is 'side_effect' (arbitrary MCP tool effects)."""
    assert CALL_MCP_TOOL.purity == "side_effect"


def test_list_mcp_servers_purity_read_only():
    """Tier 2: LIST_MCP_SERVERS purity is 'read_only' (pure config enumeration)."""
    assert LIST_MCP_SERVERS.purity == "read_only"


def test_list_mcp_tools_purity_read_only():
    """Tier 2: LIST_MCP_TOOLS purity is 'read_only' (queries server for tool listing)."""
    assert LIST_MCP_TOOLS.purity == "read_only"


def test_all_mcp_tools_category_discovery():
    """Tier 2: All 3 MCP ToolDefinitions have category='discovery'."""
    assert CALL_MCP_TOOL.category == "discovery"
    assert LIST_MCP_SERVERS.category == "discovery"
    assert LIST_MCP_TOOLS.category == "discovery"


# ── 6. render_for_phase shape ─────────────────────────────────────────────────

def test_call_mcp_tool_render_for_phase_shape():
    """Tier 2: CALL_MCP_TOOL.render_for_phase() has kind, description, args_schema,
    purity with correct FP-0032 values (mcp_tool_name instead of tool)."""
    rendered = CALL_MCP_TOOL.render_for_phase()
    assert rendered["kind"] == "call_mcp_tool"
    assert rendered["description"] == _CALL_MCP_TOOL_DESCRIPTION
    assert "server" in rendered["args_schema"]["properties"]
    assert "mcp_tool_name" in rendered["args_schema"]["properties"]
    assert "tool" not in rendered["args_schema"]["properties"], (
        "Legacy 'tool' key must not appear in render_for_phase — FP-0032 rename"
    )
    assert rendered["args_schema"]["properties"]["args"] == {"type": "object"}
    assert rendered["purity"] == "side_effect"


def test_list_mcp_servers_render_for_phase_shape():
    """Tier 2: LIST_MCP_SERVERS.render_for_phase() has kind='list_mcp_servers'
    and correct description and args_schema."""
    rendered = LIST_MCP_SERVERS.render_for_phase()
    assert rendered["kind"] == "list_mcp_servers"
    assert rendered["description"] == _LIST_MCP_SERVERS_DESCRIPTION
    assert rendered["args_schema"]["properties"] == {}
    assert rendered["purity"] == "read_only"


def test_list_mcp_tools_render_for_phase_shape():
    """Tier 2: LIST_MCP_TOOLS.render_for_phase() has kind='list_mcp_tools'
    and correct description and args_schema."""
    rendered = LIST_MCP_TOOLS.render_for_phase()
    assert rendered["kind"] == "list_mcp_tools"
    assert rendered["description"] == _LIST_MCP_TOOLS_DESCRIPTION
    assert rendered["args_schema"]["properties"]["server"] == {"type": "string"}
    assert rendered["purity"] == "read_only"


# ── 7. Registry gate filtering ────────────────────────────────────────────────

def test_all_three_appear_in_for_router():
    """Tier 2: All 3 MCP ToolDefinitions appear in for_router() (gates.router=allow)."""
    registry = ToolRegistry()
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
    registry.register(LIST_MCP_TOOLS)

    router_list = registry.for_router()
    assert CALL_MCP_TOOL in router_list
    assert LIST_MCP_SERVERS in router_list
    assert LIST_MCP_TOOLS in router_list


def test_all_three_appear_in_for_phase():
    """Tier 2: All 3 MCP ToolDefinitions appear in for_phase() (gates.phase=allow).
    This is the Type C closure invariant — phase side now has access to all MCP
    discover capabilities."""
    registry = ToolRegistry()
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
    registry.register(LIST_MCP_TOOLS)

    phase_list = registry.for_phase()
    assert CALL_MCP_TOOL in phase_list
    assert LIST_MCP_SERVERS in phase_list
    assert LIST_MCP_TOOLS in phase_list


def test_registry_lookup_by_name():
    """Tier 2: Registry lookup by canonical name returns the correct ToolDefinition."""
    registry = ToolRegistry()
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
    registry.register(LIST_MCP_TOOLS)

    assert registry.lookup("call_mcp_tool") is CALL_MCP_TOOL
    assert registry.lookup("list_mcp_servers") is LIST_MCP_SERVERS
    assert registry.lookup("list_mcp_tools") is LIST_MCP_TOOLS


# ── 8. Polymorphic args contract for call_mcp_tool ───────────────────────────

def test_call_mcp_tool_args_schema_accepts_object():
    """Tier 2: call_mcp_tool args parameter schema is {"type": "object"}, making
    the dynamic MCP tool space polymorphic — any JSON object can be passed as args.
    This is the key invariant that allows arbitrary MCP servers/tools without
    OS-level changes (P7 compliance)."""
    params = dict(CALL_MCP_TOOL.parameters)
    assert params["properties"]["args"] == {"type": "object"}
    # No additionalProperties constraint — fully open to arbitrary MCP tool args
    assert "additionalProperties" not in params["properties"]["args"]


def test_call_mcp_tool_required_fields():
    """Tier 2: call_mcp_tool requires exactly server + mcp_tool_name + args
    (FP-0032 vocabulary: 'tool' renamed to 'mcp_tool_name')."""
    params = dict(CALL_MCP_TOOL.parameters)
    assert set(params["required"]) == {"server", "mcp_tool_name", "args"}, (
        "FP-0032: 'tool' must be replaced by 'mcp_tool_name' in required fields"
    )


def test_call_mcp_tool_render_for_router_top_level_shape():
    """Tier 2: CALL_MCP_TOOL.render_for_router() top-level shape is {type: function,
    function: {...}}. Guards the OpenAI tools[] format contract."""
    rendered = CALL_MCP_TOOL.render_for_router()
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)
    fn = rendered["function"]
    assert "name" in fn
    assert "description" in fn
    assert "parameters" in fn
