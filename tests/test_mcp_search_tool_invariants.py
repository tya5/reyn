"""Tier 2: FP-0024 Component D — Anthropic tool_search_tool MCP integration.

Invariant tests for the threshold-based MCP tool-search switch introduced in
router_tools.build_tools() (FP-0024 Component D).

Three Tier 2 OS-invariant tests:
1. test_below_threshold_uses_inline_mcp_tools
   < threshold MCP servers → existing D1–D3 inline tools present; no search tool.
2. test_above_threshold_uses_search_tool
   >= threshold MCP servers → tool_search_tool present; D1–D3 NOT present.
3. test_search_tool_structure
   tool_search_tool output carries the required Anthropic meta-tool fields.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real build_tools() invocation; real MCP_SEARCH_THRESHOLD constant.
- No assertion on internal private state.
"""
from __future__ import annotations

from reyn.core.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.runtime.router_tools import MCP_SEARCH_THRESHOLD, build_mcp_search_tool, build_tools

# ── Shared fixtures ───────────────────────────────────────────────────────────

_SAMPLE_SKILLS = [{"name": "skill_a", "description": "A skill"}]
_SAMPLE_AGENTS = [{"name": "agent_a", "role": "Agent A"}]

_INLINE_MCP_TOOL_NAMES = {"list_mcp_servers", "list_mcp_tools", "call_mcp_tool"}


def _make_mcp_servers(count: int) -> list[dict]:
    """Return a list of `count` minimal MCP server dicts."""
    return [
        {"name": f"server_{i}", "description": f"MCP server {i}"}
        for i in range(count)
    ]


def _tool_names(tools: list[dict]) -> set[str]:
    """Return tool names from a tools list, handling both OpenAI and raw Anthropic shapes."""
    names: set[str] = set()
    for t in tools:
        if t.get("type") == "function" and "function" in t:
            names.add(t["function"]["name"])
        elif "name" in t:
            # Anthropic meta-tool shape: {type: "tool_search_tool_*", name: ..., ...}
            names.add(t["name"])
    return names


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_below_threshold_uses_inline_mcp_tools():
    """Tier 2: build_tools() < explicit threshold → inline D1–D4 MCP tools present; no search tool.

    FP-0024 Component D backward-compat invariant: when MCP server count
    is strictly below mcp_search_threshold, behavior is identical to pre-FP-0024
    (D1–D4 inline tools, no tool_search_tool entry in the catalog).

    FP-0032: MCP_SEARCH_THRESHOLD is now 0 (default-off for Anthropic-specific
    tool_search_tool). This test uses an explicit threshold > 0 to test the
    threshold gate logic.
    """
    # Use an explicit threshold to test the threshold-gate logic.
    # FP-0032: MCP_SEARCH_THRESHOLD defaults to 0 (opt-in only); use 5 here.
    _explicit_threshold = 5
    below_count = max(1, _explicit_threshold - 1)
    servers = _make_mcp_servers(below_count)

    tools = build_tools(
        _SAMPLE_SKILLS,
        _SAMPLE_AGENTS,
        mcp_servers=servers,
        mcp_search_threshold=_explicit_threshold,
    )
    names = _tool_names(tools)

    # All four inline MCP tools must be present (FP-0032 adds describe_mcp_tool).
    _INLINE_MCP_TOOL_NAMES_FP0032 = _INLINE_MCP_TOOL_NAMES | {"describe_mcp_tool"}
    missing = _INLINE_MCP_TOOL_NAMES_FP0032 - names
    assert not missing, (
        f"Inline MCP tools missing below threshold (count={below_count}, "
        f"threshold={_explicit_threshold}): {missing}. Got names: {names}"
    )

    # tool_search_tool must NOT be present.
    assert "tool_search" not in names, (
        f"tool_search unexpectedly present below threshold "
        f"(count={below_count}, threshold={_explicit_threshold})"
    )


def test_above_threshold_uses_search_tool():
    """Tier 2: build_tools() >= explicit threshold → tool_search_tool present; inline D1–D3 absent.

    FP-0024 Component D core invariant: when MCP server count meets or exceeds
    an explicit mcp_search_threshold > 0, the catalog contains the tool_search_tool
    meta-tool and none of the individual D1–D3 MCP management tools.

    FP-0032: MCP_SEARCH_THRESHOLD is now 0 (Anthropic tool_search_tool default-off).
    Callers must opt in by passing mcp_search_threshold > 0. This test validates the
    threshold gate still works when explicitly set.
    """
    # Use an explicit threshold to test the threshold-gate logic.
    _explicit_threshold = 5
    at_count = _explicit_threshold
    servers = _make_mcp_servers(at_count)

    tools = build_tools(
        _SAMPLE_SKILLS,
        _SAMPLE_AGENTS,
        mcp_servers=servers,
        mcp_search_threshold=_explicit_threshold,
    )
    names = _tool_names(tools)

    # tool_search meta-tool must be present.
    assert "tool_search" in names, (
        f"tool_search missing at explicit threshold "
        f"(count={at_count}, threshold={_explicit_threshold}). Got names: {names}"
    )

    # Inline D1–D3 tools must NOT be present (schema-load savings require exclusion).
    unexpected_inline = _INLINE_MCP_TOOL_NAMES & names
    assert not unexpected_inline, (
        f"Inline D1–D3 MCP tools unexpectedly present alongside tool_search "
        f"(count={at_count}): {unexpected_inline}"
    )


def test_search_tool_structure():
    """Tier 2: build_mcp_search_tool() output carries required Anthropic meta-tool fields.

    Validates the structural contract of the tool_search_tool spec:
    - ``type`` field identifies it as the Anthropic deferred-loading meta-tool
    - ``name`` is the callable name the LLM will use ("tool_search")
    - ``max_results`` is a positive integer
    - ``tools`` is a list (may be empty or populated)

    Also verifies that FP-0024 event kinds are declared in EVENT_AUDIT_REQUIREMENTS
    (P6 audit trail completeness for mcp_search_invoked and mcp_tool_loaded).
    """
    stub_tools = [
        {
            "type": "function",
            "function": {
                "name": "fs_read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    result = build_mcp_search_tool(stub_tools)

    # ``type`` must identify the Anthropic deferred-loading tool type.
    # TODO(fp-0024-d): verify exact version string against Anthropic SDK docs.
    assert "type" in result, "build_mcp_search_tool() result missing 'type' field"
    assert result["type"].startswith("tool_search_tool_"), (
        f"Expected type to start with 'tool_search_tool_', got: {result['type']!r}"
    )

    # ``name`` is the callable name the LLM uses.
    assert result.get("name") == "tool_search", (
        f"Expected name='tool_search', got: {result.get('name')!r}"
    )

    # ``max_results`` must be a positive integer.
    assert isinstance(result.get("max_results"), int), (
        f"Expected max_results to be int, got: {type(result.get('max_results'))}"
    )
    assert result["max_results"] > 0, (
        f"Expected max_results > 0, got: {result['max_results']}"
    )

    # ``tools`` must be a list.
    assert isinstance(result.get("tools"), list), (
        f"Expected tools to be list, got: {type(result.get('tools'))}"
    )
    assert len(result["tools"]) == len(stub_tools), (
        f"Expected {len(stub_tools)} tool(s) in tools array, "
        f"got {len(result['tools'])}"
    )

    # P6 audit completeness: FP-0024 event kinds must be declared.
    assert "mcp_search_invoked" in EVENT_AUDIT_REQUIREMENTS, (
        "FP-0024 P6: 'mcp_search_invoked' missing from EVENT_AUDIT_REQUIREMENTS"
    )
    assert "mcp_tool_loaded" in EVENT_AUDIT_REQUIREMENTS, (
        "FP-0024 P6: 'mcp_tool_loaded' missing from EVENT_AUDIT_REQUIREMENTS"
    )
    # Required fields for each event kind.
    assert EVENT_AUDIT_REQUIREMENTS["mcp_search_invoked"] >= frozenset({"query", "result_count"}), (
        "mcp_search_invoked missing required audit fields"
    )
    assert EVENT_AUDIT_REQUIREMENTS["mcp_tool_loaded"] >= frozenset({"tool_name", "server_name"}), (
        "mcp_tool_loaded missing required audit fields"
    )
