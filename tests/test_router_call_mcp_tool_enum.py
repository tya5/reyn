"""Tier 2: FP-0032 MCP catalog parity — enum injection + describe_mcp_tool + SP flat list.

Verifies the three-layer affordance control gap between skill/agent and MCP is closed:
  1. Schema enum: call_mcp_tool.server and call_mcp_tool.mcp_tool_name get enum injection.
  2. SP flat list: system prompt contains "## MCP servers and tools" flat listing.
  3. Describe helper: describe_mcp_tool is registered and returns full input_schema.

Also pins:
  - Vocabulary unification: 'tool' → 'mcp_tool_name' in call_mcp_tool parameters.
  - list_mcp_tools returns 'mcp_tools' key (not 'tools') and omits inputSchema.
  - MCP_SEARCH_THRESHOLD == 0 (Anthropic tool_search_tool default-off).
  - describe_mcp_tool has same enum injection as call_mcp_tool (symmetry invariant).

Pattern source: tests/test_router_invoke_skill_enum.py.
Policy: Tier 2, real instances, no unittest.mock / MagicMock / AsyncMock / patch.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import MCP_SEARCH_THRESHOLD, build_tools
from reyn.tools import get_default_registry
from reyn.tools.mcp import (
    DESCRIBE_MCP_TOOL,
    _handle_list_mcp_tools,
)
from reyn.tools.types import RouterCallerState, ToolContext

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_SKILLS = [{"name": "direct_llm", "description": "Direct LLM call", "category": "general"}]
_AGENTS = [{"name": "researcher", "role": "Research agent", "cluster": "default"}]
_EMPTY_MEMORY: dict = {"status": "not_found", "content": ""}

_MCP_SERVERS_NO_TOOLS = [
    {"name": "brave", "description": "Brave Search MCP"},
    {"name": "github", "description": "GitHub MCP"},
]

_MCP_SERVERS_WITH_TOOLS = [
    {
        "name": "brave",
        "description": "Brave Search MCP",
        "tools": [
            {"name": "search", "description": "Search the web", "inputSchema": {"type": "object"}},
            {"name": "news", "description": "News search", "inputSchema": {"type": "object"}},
        ],
    },
    {
        "name": "github",
        "description": "GitHub MCP",
        "tools": [
            {"name": "create_issue", "description": "Create a new issue", "inputSchema": {"type": "object"}},
        ],
    },
]


def _get_tool(tools: list[dict], name: str) -> dict | None:
    """Return the function-level dict for the named tool, or None."""
    for t in tools:
        if t.get("type") == "function" and t["function"]["name"] == name:
            return t["function"]
    return None


# ---------------------------------------------------------------------------
# (1) Vocabulary unification: 'tool' → 'mcp_tool_name'
# ---------------------------------------------------------------------------


def test_call_mcp_tool_param_renamed_to_mcp_tool_name():
    """Tier 2: call_mcp_tool.parameters uses 'mcp_tool_name', not 'tool'.

    FP-0032 vocabulary unification: the old 'tool' param collided with OpenAI's
    standard 'tool' semantics. 'mcp_tool_name' is unambiguous.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_NO_TOOLS)
    fn = _get_tool(tools, "call_mcp_tool")
    assert fn is not None, "call_mcp_tool must be present when mcp_servers are configured"
    props = fn["parameters"]["properties"]
    assert "mcp_tool_name" in props, (
        "call_mcp_tool must have 'mcp_tool_name' parameter (FP-0032 rename)"
    )
    assert "tool" not in props, (
        "Legacy 'tool' param must be absent from call_mcp_tool (FP-0032 rename)"
    )
    assert "mcp_tool_name" in fn["parameters"]["required"], (
        "'mcp_tool_name' must be in required fields"
    )


# ---------------------------------------------------------------------------
# (2) Schema enum injection: server
# ---------------------------------------------------------------------------


def test_call_mcp_tool_server_enum_injected():
    """Tier 2: call_mcp_tool.server gets an enum from configured mcp_servers.

    P4 alignment: LLM can only pick from OS-provided server names.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_NO_TOOLS)
    fn = _get_tool(tools, "call_mcp_tool")
    assert fn is not None
    server_schema = fn["parameters"]["properties"]["server"]
    assert "enum" in server_schema, (
        "call_mcp_tool.server must have an enum when mcp_servers are configured"
    )
    assert set(server_schema["enum"]) == {"brave", "github"}, (
        f"server enum must match mcp_servers names; got {server_schema['enum']}"
    )


# ---------------------------------------------------------------------------
# (3) Schema enum injection: mcp_tool_name (dotted form)
# ---------------------------------------------------------------------------


def test_call_mcp_tool_mcp_tool_name_enum_injected_when_tools_available():
    """Tier 2: call_mcp_tool.mcp_tool_name gets an enum in dotted form when
    mcp_servers entries carry a 'tools' list.

    Dotted form <server>.<tool> avoids name collision across servers.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_WITH_TOOLS)
    fn = _get_tool(tools, "call_mcp_tool")
    assert fn is not None
    tool_name_schema = fn["parameters"]["properties"]["mcp_tool_name"]
    assert "enum" in tool_name_schema, (
        "call_mcp_tool.mcp_tool_name must have an enum when server tool listings are available"
    )
    expected = {"brave.search", "brave.news", "github.create_issue"}
    assert set(tool_name_schema["enum"]) == expected, (
        f"mcp_tool_name enum must use dotted form; got {tool_name_schema['enum']}"
    )


def test_call_mcp_tool_no_mcp_tool_name_enum_when_no_tools_listed():
    """Tier 2: call_mcp_tool.mcp_tool_name is plain string when mcp_servers
    don't carry tool listings (common: async enumeration not done yet).

    Graceful fallback: schema remains valid, no empty enum.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_NO_TOOLS)
    fn = _get_tool(tools, "call_mcp_tool")
    assert fn is not None
    tool_name_schema = fn["parameters"]["properties"]["mcp_tool_name"]
    assert "enum" not in tool_name_schema, (
        "call_mcp_tool.mcp_tool_name must NOT have an enum when no tool listings are present "
        f"(got: {tool_name_schema})"
    )
    assert tool_name_schema.get("type") == "string"


# ---------------------------------------------------------------------------
# (4) No enum when no mcp_servers configured
# ---------------------------------------------------------------------------


def test_call_mcp_tool_not_present_when_no_mcp_servers():
    """Tier 2: call_mcp_tool is absent from the tool list when mcp_servers=[].

    Same pattern as invoke_skill being absent when available_skills=[].
    An empty-server catalog should not present MCP tools at all.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=[])
    names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    assert "call_mcp_tool" not in names, (
        "call_mcp_tool must be absent when mcp_servers is empty"
    )
    assert "describe_mcp_tool" not in names, (
        "describe_mcp_tool must be absent when mcp_servers is empty"
    )


# ---------------------------------------------------------------------------
# (5) describe_mcp_tool registered in default registry
# ---------------------------------------------------------------------------


def test_describe_mcp_tool_registered_in_default_registry():
    """Tier 2: describe_mcp_tool appears in get_default_registry() (D4 registration).

    Mirrors test for describe_skill registration. Missing registration means
    the tool is unreachable from the router dispatch path.
    """
    registry = get_default_registry()
    describe_def = registry.lookup("describe_mcp_tool")
    assert describe_def is not None, (
        "describe_mcp_tool must be registered in get_default_registry()"
    )
    assert describe_def.name == "describe_mcp_tool"
    assert describe_def.gates.router == "allow"
    assert describe_def.gates.phase == "allow"


# ---------------------------------------------------------------------------
# (6) describe_mcp_tool handler returns input_schema
# ---------------------------------------------------------------------------


def test_describe_mcp_tool_handler_returns_input_schema():
    """Tier 2: describe_mcp_tool handler returns {name, description, input_schema}
    for a valid mcp_tool_name via a fake host adapter.

    Uses a real ToolContext + real handler; no mocks.
    """
    class _FakeHost:
        async def mcp_list_tools(self, server: str) -> list[dict]:
            return [
                {
                    "name": "search",
                    "description": "Search the web",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            ]
        async def mcp_list_servers(self) -> list[dict]:
            return [{"name": "brave", "description": "Brave"}]
        async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
            return {}

    host = _FakeHost()
    rs = RouterCallerState(host=host)
    ctx = ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=rs,
    )

    result = asyncio.run(
        DESCRIBE_MCP_TOOL.handler(
            {"server": "brave", "mcp_tool_name": "brave.search"},
            ctx,
        )
    )
    assert "error" not in result, f"describe_mcp_tool returned error: {result}"
    assert result.get("name") == "search"
    assert "input_schema" in result, "describe_mcp_tool must return 'input_schema' key"
    assert result["input_schema"] == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }


# ---------------------------------------------------------------------------
# (7) describe_mcp_tool enum mirrors call_mcp_tool (symmetry invariant)
# ---------------------------------------------------------------------------


def test_describe_mcp_tool_enum_mirrors_call_mcp_tool():
    """Tier 2: describe_mcp_tool.server + mcp_tool_name enums are identical to
    call_mcp_tool's enums when mcp_servers carry tool listings.

    Symmetry invariant: both tools constrain LLM to the same candidate set.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_WITH_TOOLS)
    call_fn = _get_tool(tools, "call_mcp_tool")
    describe_fn = _get_tool(tools, "describe_mcp_tool")
    assert call_fn is not None, "call_mcp_tool must be present"
    assert describe_fn is not None, "describe_mcp_tool must be present"

    call_server_enum = call_fn["parameters"]["properties"]["server"].get("enum")
    describe_server_enum = describe_fn["parameters"]["properties"]["server"].get("enum")
    assert call_server_enum == describe_server_enum, (
        f"server enum mismatch: call={call_server_enum}, describe={describe_server_enum}"
    )

    call_tool_enum = call_fn["parameters"]["properties"]["mcp_tool_name"].get("enum")
    describe_tool_enum = describe_fn["parameters"]["properties"]["mcp_tool_name"].get("enum")
    assert call_tool_enum == describe_tool_enum, (
        f"mcp_tool_name enum mismatch: call={call_tool_enum}, describe={describe_tool_enum}"
    )


# ---------------------------------------------------------------------------
# (8) list_mcp_tools returns 'mcp_tools' key
# ---------------------------------------------------------------------------


def test_list_mcp_tools_returns_mcp_tools_key():
    """Tier 2: _handle_list_mcp_tools returns {'mcp_tools': [...]} not {'tools': [...]}.

    FP-0032 root-cause fix: 'tools' key collided with OpenAI tool-definition shape,
    causing the LLM to interpret listed mcp_tools as top-level callables.
    """
    class _FakeHost:
        async def mcp_list_tools(self, server: str) -> list[dict]:
            return [{"name": "search", "description": "Search", "inputSchema": {}}]
        async def mcp_list_servers(self) -> list[dict]:
            return []
        async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
            return {}

    host = _FakeHost()
    rs = RouterCallerState(host=host)
    ctx = ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=rs,
    )

    result = asyncio.run(
        _handle_list_mcp_tools({"server": "brave"}, ctx)
    )
    assert "mcp_tools" in result, (
        f"list_mcp_tools must return 'mcp_tools' key (FP-0032); got: {list(result.keys())}"
    )
    assert "tools" not in result, (
        "list_mcp_tools must NOT return legacy 'tools' key (FP-0032 rename)"
    )


# ---------------------------------------------------------------------------
# (9) list_mcp_tools omits inputSchema from each entry
# ---------------------------------------------------------------------------


def test_list_mcp_tools_omits_input_schema():
    """Tier 2: each entry in mcp_tools result has no 'inputSchema' key.

    FP-0032 structural fix: inputSchema presence makes the entry structurally
    identical to an OpenAI function tool definition, causing the LLM to treat
    mcp_tools as directly callable. Removing inputSchema breaks the false affordance.
    Full schema is available via describe_mcp_tool.
    """
    class _FakeHost:
        async def mcp_list_tools(self, server: str) -> list[dict]:
            return [
                {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
                {"name": "news", "description": "News", "inputSchema": {"type": "object"}},
            ]
        async def mcp_list_servers(self) -> list[dict]:
            return []
        async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
            return {}

    host = _FakeHost()
    rs = RouterCallerState(host=host)
    ctx = ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=rs,
    )

    result = asyncio.run(
        _handle_list_mcp_tools({"server": "brave"}, ctx)
    )
    for entry in result["mcp_tools"]:
        assert "inputSchema" not in entry, (
            f"list_mcp_tools entry must not contain 'inputSchema' (FP-0032); got: {entry}"
        )
        assert "name" in entry, "Each mcp_tools entry must have 'name'"


# ---------------------------------------------------------------------------
# (10) System prompt "## MCP servers and tools" flat list
# ---------------------------------------------------------------------------


def test_system_prompt_renders_flat_mcp_tool_list():
    """Tier 2: system prompt contains '## MCP servers and tools' section with
    flat dotted-form tool listing when mcp_servers carry tool info.

    Mirrors the 'Available skills' flat list pattern — provides the LLM with
    context layer alongside the schema-layer enum constraint.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
        mcp_servers=_MCP_SERVERS_WITH_TOOLS,
    )
    assert "## MCP servers and tools" in prompt, (
        "SP must contain '## MCP servers and tools' section header"
    )
    # Dotted form tool names must appear in the flat list
    assert "brave.search" in prompt, (
        "'brave.search' dotted tool name must appear in SP MCP flat list"
    )
    assert "brave.news" in prompt, (
        "'brave.news' dotted tool name must appear in SP MCP flat list"
    )
    assert "github.create_issue" in prompt, (
        "'github.create_issue' dotted tool name must appear in SP MCP flat list"
    )


def test_system_prompt_mcp_section_fallback_when_no_tool_list():
    """Tier 2: when mcp_servers have no 'tools' list, SP shows server-level entry
    with hint to use list_mcp_tools to discover mcp_tools.

    Graceful fallback: server is still surfaced; hint replaces the flat list.
    """
    prompt = build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=_SKILLS,
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
        mcp_servers=_MCP_SERVERS_NO_TOOLS,
    )
    assert "## MCP servers and tools" in prompt, (
        "SP must still show MCP section even when no tool listings available"
    )
    assert "brave" in prompt, "'brave' server name must appear in SP MCP section"
    assert "list_mcp_tools" in prompt, (
        "SP must hint 'list_mcp_tools' when mcp_tools are not pre-listed"
    )


# ---------------------------------------------------------------------------
# (11) MCP_SEARCH_THRESHOLD defaults to 0
# ---------------------------------------------------------------------------


def test_mcp_search_threshold_defaults_to_zero():
    """Tier 2: MCP_SEARCH_THRESHOLD == 0 (FP-0032 default-off for Anthropic tool_search_tool).

    Anthropic's tool_search_tool is provider-specific; Reyn's default must be
    provider-agnostic. Threshold 0 means always inline (no tool_search_tool).
    Operators can opt in by setting mcp.search_threshold > 0 in reyn.yaml.
    """
    assert MCP_SEARCH_THRESHOLD == 0, (
        f"MCP_SEARCH_THRESHOLD must be 0 (FP-0032 default-off); got {MCP_SEARCH_THRESHOLD}"
    )


def test_default_threshold_always_inline():
    """Tier 2: with MCP_SEARCH_THRESHOLD=0 (default), build_tools() always inlines
    D1–D4 regardless of server count — no tool_search_tool unless explicit opt-in.
    """
    # Many servers — with threshold=0, always inline
    many_servers = [
        {"name": f"server_{i}", "description": f"Server {i}"}
        for i in range(50)
    ]
    tools = build_tools(
        _SKILLS,
        _AGENTS,
        mcp_servers=many_servers,
        mcp_search_threshold=MCP_SEARCH_THRESHOLD,  # == 0
    )
    names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    assert "call_mcp_tool" in names, (
        "call_mcp_tool must be inline at threshold=0 regardless of server count"
    )
    assert "describe_mcp_tool" in names, (
        "describe_mcp_tool must be inline at threshold=0 regardless of server count"
    )
    tool_search_tools = [t for t in tools if t.get("type", "").startswith("tool_search_tool")]
    assert not tool_search_tools, (
        "tool_search_tool must NOT appear when threshold=0 (default-off)"
    )


# ---------------------------------------------------------------------------
# (12) describe_mcp_tool present in build_tools output
# ---------------------------------------------------------------------------


def test_describe_mcp_tool_present_in_build_tools():
    """Tier 2: describe_mcp_tool appears in build_tools() output when mcp_servers
    are configured (D4 alongside D1–D3).

    Closes the symmetry gap: skill has describe_skill, agent has describe_agent,
    MCP now has describe_mcp_tool.
    """
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=_MCP_SERVERS_NO_TOOLS)
    names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    assert "describe_mcp_tool" in names, (
        "describe_mcp_tool (D4) must appear in build_tools output alongside D1–D3"
    )


def test_describe_mcp_tool_absent_when_no_mcp_servers():
    """Tier 2: describe_mcp_tool is absent when mcp_servers=[] (same guard as D1–D3)."""
    tools = build_tools(_SKILLS, _AGENTS, mcp_servers=None)
    names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    assert "describe_mcp_tool" not in names, (
        "describe_mcp_tool must be absent when no mcp_servers configured"
    )
