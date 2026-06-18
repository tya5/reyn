"""Tier 2: ToolSpec dataclass invariants (ToolSpec refactor, PR-next).

Three contract tests:
  1. ToolSpec.to_openai_dict() round-trip produces a valid OpenAI tool shape.
  2. build_tools() specs are consistent with get_dispatch_kind().
  3. Unknown tool name defaults to "sync" from get_dispatch_kind().

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

_SAMPLE_SKILLS = [{"name": "example_skill", "description": "An example skill"}]
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
    tools = build_tools(_SAMPLE_SKILLS, _SAMPLE_AGENTS)
    tool_names = [t["function"]["name"] for t in tools]

    # These two are always present and must be "async".
    assert "delegate_to_agent" in tool_names, "delegate_to_agent must always be present"
    assert "plan" in tool_names, "plan must always be present"
    assert get_dispatch_kind("delegate_to_agent") == "async"
    assert get_dispatch_kind("plan") == "async"

    # All other tools in the baseline set must be "sync".
    async_tools = {"delegate_to_agent", "plan"}
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
        _SAMPLE_SKILLS,
        _SAMPLE_AGENTS,
        file_permissions={"read": ["src"], "write": ["out"]},
        mcp_servers=[{"name": "fs", "description": "FS"}],
        web_fetch_allowed=True,
    )
    tool_names = [t["function"]["name"] for t in tools]
    async_tools = {"delegate_to_agent", "plan"}
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
    assert get_dispatch_kind("list_skills") == "sync"
    assert get_dispatch_kind("invoke_skill") == "sync"
    assert get_dispatch_kind("web_search") == "sync"
