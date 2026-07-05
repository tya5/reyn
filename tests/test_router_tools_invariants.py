"""Tier 2: ToolSpec dataclass invariants (ToolSpec refactor, PR-next).

Four contract tests:
  1. ToolSpec.to_openai_dict() round-trip produces a valid OpenAI tool shape.
  2. build_tools() specs are consistent with get_dispatch_kind().
  3. Unknown tool name defaults to "sync" from get_dispatch_kind().
  4. build_tools() advertises every router=allow MCP verb (#2597 regression
     guard — resources/subscribe/prompts verbs were registered + dispatchable
     but not wired into the live tool schema).

No mocks. No private-state assertions.
"""

from __future__ import annotations

import pytest

from reyn.runtime.router_tools import (
    ToolSpec,
    build_tools,
    get_dispatch_kind,
)

# ── shared fixtures ───────────────────────────────────────────────────────────

_SAMPLE_AGENTS = [{"name": "peer_agent", "role": "Peer"}]


# ── 1. ToolSpec round-trip to OpenAI shape ────────────────────────────────────


def test_toolspec_to_openai_dict_shape() -> None:
    """Tier 2: ToolSpec.to_openai_dict() produces a valid OpenAI tool schema.

    OpenAI tool schema contract:
      - top-level key "type" == "function"
      - nested "function" dict with "name", "description", "parameters"
    """
    spec = ToolSpec(
        name="test_tool",
        description="A test tool description.",
        parameters={
            "type": "object",
            "properties": {"arg": {"type": "string"}},
            "required": ["arg"],
        },
        dispatch_kind="sync",
    )
    result = spec.to_openai_dict()

    assert result["type"] == "function", "top-level type must be 'function'"
    fn = result["function"]
    assert fn["name"] == "test_tool"
    assert fn["description"] == "A test tool description."
    assert fn["parameters"] == {
        "type": "object",
        "properties": {"arg": {"type": "string"}},
        "required": ["arg"],
    }
    # dispatch_kind is NOT in the OpenAI wire format — it's OS-internal metadata.
    assert "dispatch_kind" not in result
    assert "dispatch_kind" not in fn


def test_toolspec_to_openai_dict_async_tool() -> None:
    """Tier 2: async ToolSpec also round-trips correctly; dispatch_kind
    is retained on the dataclass but absent from the OpenAI wire format."""
    spec = ToolSpec(
        name="async_tool",
        description="An async tool.",
        parameters={"type": "object", "properties": {}, "required": []},
        dispatch_kind="async",
    )
    assert spec.dispatch_kind == "async"
    result = spec.to_openai_dict()
    assert result["type"] == "function"
    assert result["function"]["name"] == "async_tool"
    # Wire format must not leak dispatch_kind.
    assert "dispatch_kind" not in result
    assert "dispatch_kind" not in result["function"]


def test_toolspec_frozen() -> None:
    """Tier 2: ToolSpec is immutable (frozen=True); mutation raises TypeError."""
    spec = ToolSpec(
        name="immutable",
        description="Frozen spec.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    with pytest.raises((TypeError, AttributeError)):
        spec.name = "mutated"  # type: ignore[misc]


# ── 2. build_tools() dispatch_kinds match get_dispatch_kind() ─────────────────


def test_build_tools_dispatch_kinds_consistent() -> None:
    """Tier 2: every ToolSpec used in build_tools() must match what
    get_dispatch_kind(name) returns.

    Verified via build_tools() output: the returned OpenAI dicts carry
    the tool names; we confirm get_dispatch_kind() is consistent with
    the known async tools (delegate_to_agent, plan) and that all others
    default to "sync".
    """
    tools = build_tools(_SAMPLE_AGENTS)
    tool_names = [t["function"]["name"] for t in tools]

    # delegate_to_agent is always present and must be "async".
    assert "delegate_to_agent" in tool_names, "delegate_to_agent must always be present"
    assert get_dispatch_kind("delegate_to_agent") == "async"

    # All other tools in the baseline set must be "sync".
    async_tools = {"delegate_to_agent", "session_spawn"}
    for name in tool_names:
        expected = "async" if name in async_tools else "sync"
        actual = get_dispatch_kind(name)
        assert actual == expected, (
            f"dispatch_kind mismatch for '{name}': expected {expected!r}, got {actual!r}"
        )


def test_build_tools_full_permissions_dispatch_kinds_consistent() -> None:
    """Tier 2: with file + MCP + web_fetch enabled, newly included tools
    (list_directory, read_file, write_file, delete_file, list_mcp_servers,
    list_mcp_tools, call_mcp_tool, web_fetch) must all be 'sync'."""
    tools = build_tools(
        _SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=[{"name": "fs", "description": "FS"}],
        web_fetch_allowed=True,
    )
    tool_names = [t["function"]["name"] for t in tools]
    async_tools = {"delegate_to_agent", "session_spawn"}
    for name in tool_names:
        expected = "async" if name in async_tools else "sync"
        actual = get_dispatch_kind(name)
        assert actual == expected, (
            f"dispatch_kind mismatch for '{name}': expected {expected!r}, got {actual!r}"
        )


# ── 3. Unknown tool name defaults to "sync" ───────────────────────────────────


def test_get_dispatch_kind_unknown_defaults_to_sync() -> None:
    """Tier 2: get_dispatch_kind() returns "sync" for any tool name not
    explicitly registered as "async". This is the safe default — an
    unregistered tool will not cause RouterLoop to exit prematurely."""
    assert get_dispatch_kind("no_such_tool") == "sync"
    assert get_dispatch_kind("") == "sync"


# ── 4. All MCP verbs actually reach the live tool schema ──────────────────────
#
# #2597 regression guard: list_mcp_resources, list_mcp_resource_templates,
# read_mcp_resource, subscribe_mcp_resource, unsubscribe_mcp_resource,
# list_mcp_prompts, get_mcp_prompt were registered in get_default_registry()
# with gates.router="allow" and were fully dispatchable, but build_tools()
# had ZERO wiring for them (grep-confirmed) — a real chat agent could never
# see or call them (same advertising-gap class as #2589/#2555/#2120). This
# test builds the live tool schema via build_tools() with an MCP server
# configured and asserts every MCP verb (the existing D1-D4 plus the new
# D5-D11 seven) is present by name. It fails on pre-fix code (7 missing
# names) and passes post-fix. Uses the real ToolRegistry — no mocks.


def test_build_tools_advertises_all_mcp_verbs() -> None:
    """Tier 2: build_tools() must advertise every router=allow MCP verb.

    Regression guard for the #2597 advertising gap: resources / resource
    templates / subscribe / unsubscribe / prompts verbs were registered +
    dispatchable but never wired into build_tools(), so a real chat agent
    could not discover or call them even though the handlers worked.
    """
    tools = build_tools(
        _SAMPLE_AGENTS,
        mcp_servers=[{"name": "fs", "description": "FS"}],
    )
    tool_names = {t["function"]["name"] for t in tools}

    expected_mcp_verbs = {
        # D1-D4: pre-existing MCP tool-consumption verbs.
        "list_mcp_servers",
        "list_mcp_tools",
        "call_mcp_tool",
        "describe_mcp_tool",
        # D5-D11: #2597 slices ②a/②b/②c resources/subscribe/prompts verbs.
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "subscribe_mcp_resource",
        "unsubscribe_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
    }
    missing = expected_mcp_verbs - tool_names
    assert not missing, (
        f"MCP verbs registered as gates.router='allow' but absent from the "
        f"live build_tools() schema (undiscoverable by a real chat agent): "
        f"{sorted(missing)}"
    )
    assert get_dispatch_kind("list_skills") == "sync"
    assert get_dispatch_kind("invoke_skill") == "sync"
    assert get_dispatch_kind("web_search") == "sync"
