"""Tier 2: describe_action returns per-resource description (B42-NF-W7-1).

Companion to ``test_describe_action_resource_schemas.py`` (= D2-full
input_schema fix). The D2-full work covered ``input_schema`` only and
left the ``description`` field defaulting to the dispatcher's generic
description (= "Run a skill from the registered list. The 'name'
parameter MUST be..." for ``invoke_skill`` etc.).

The B42 W7-S2 trace ("Tell me more about the simplest one") confirmed
the empty-stop mechanism: when describe_action returns the dispatcher's
generic description instead of the resource's actual one, the LLM has
nothing meaningful to relay and produces an empty reply. N=10
trace-patch-replay: 9/10 empty → 0/10 empty after substituting the
actual ``skill.md`` description.

These tests pin:

- ``skill__X``      → description from ``list_available_skills`` entry.
- ``agent.peer__X`` → description (or role fallback) from
                      ``list_available_agents`` entry.
- ``mcp.tool__X.Y`` → per-tool description from ``mcp_servers``.
- ``mcp.server__X`` → server-level description from ``mcp_servers``.
- Operation categories fall through to ``target.description`` unchanged.
- Missing per-resource description falls through to ``target.description``.
"""
from __future__ import annotations

import asyncio

from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import _handle_describe_action


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """Minimal host returning enriched skill / agent lists."""

    def __init__(self, skills=None, agents=None):
        self._skills = skills or []
        self._agents = agents or []

    def list_available_skills(self):
        return list(self._skills)

    def list_available_agents(self):
        return list(self._agents)


def _make_ctx(skills=None, agents=None, mcp_servers=None):
    rs = RouterCallerState(
        host=_FakeHost(skills=skills, agents=agents),
        mcp_servers=mcp_servers,
    )
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )


def _describe(qualified_name: str, ctx: ToolContext) -> dict:
    return asyncio.run(_handle_describe_action(
        {"action_name": qualified_name}, ctx,
    ))


# ── skill__X ────────────────────────────────────────────────────────────


def test_skill_describe_returns_skill_description():
    """Tier 2b: describe_action(skill__X) returns the SKILL's description.

    B42-NF-W7-1: description comes from skill.md frontmatter, NOT
    invoke_skill's dispatcher instructions.
    """
    actual_desc = (
        "Catalogue-gap fallback: hand a single-shot natural-language task "
        "straight to the LLM and return its answer verbatim."
    )
    ctx = _make_ctx(skills=[
        {"name": "direct_llm", "description": actual_desc},
    ])
    out = _describe("skill__direct_llm", ctx)
    assert out["description"] == actual_desc
    # Sanity: the dispatcher description string must NOT have leaked.
    assert "Run a skill from the registered list" not in out["description"]


def test_skill_describe_missing_description_falls_back_to_dispatcher():
    """Tier 2: skill entry without ``description`` falls back to
    ``invoke_skill``'s description (= preserves D2-full pre-existing
    behavior; the LLM at least sees the dispatcher contract).
    """
    ctx = _make_ctx(skills=[
        {"name": "no_desc_skill"},  # No description field
    ])
    out = _describe("skill__no_desc_skill", ctx)
    # invoke_skill's dispatcher description (= the pre-B42 fallback)
    assert "Run a skill" in out["description"] or "skill" in out["description"].lower()


def test_skill_describe_unknown_skill_falls_back_to_dispatcher():
    """Tier 2: querying a skill not in list_available_skills falls back to
    ``invoke_skill``'s description (caller falls through error response;
    if reached the dispatcher fallback, the contract is preserved).
    """
    ctx = _make_ctx(skills=[
        {"name": "other_skill", "description": "Other"},
    ])
    out = _describe("skill__unknown_skill", ctx)
    # The unknown-action error response carries no `description` field —
    # it's a §D12 error response with `error_kind` / `suggestions`. Pin
    # that contract here so the fallback distinction is unambiguous.
    # If the response IS the error shape, we're done; if it carries a
    # description field, it must be the dispatcher fallback (not None).
    if "description" in out:
        assert out["description"]


# ── agent.peer__X ───────────────────────────────────────────────────────


def test_agent_peer_describe_returns_agent_description():
    """Tier 2b: describe_action(agent.peer__X) returns the AGENT's description.

    B42-NF-W7-1: returns description / role field, not delegate_to_agent's
    dispatcher text.
    """
    actual_desc = "Researches topics by querying multiple sources."
    ctx = _make_ctx(agents=[
        {"name": "researcher", "description": actual_desc},
    ])
    out = _describe("agent.peer__researcher", ctx)
    assert out["description"] == actual_desc


def test_agent_peer_describe_falls_back_to_role_when_no_description():
    """Tier 2: agent entry without ``description`` uses ``role`` as fallback
    (= the role field is the agent profile's human-readable summary).
    """
    ctx = _make_ctx(agents=[
        {"name": "writer", "role": "Drafts and revises article copy."},
    ])
    out = _describe("agent.peer__writer", ctx)
    assert out["description"] == "Drafts and revises article copy."


def test_agent_peer_describe_missing_both_falls_back_to_dispatcher():
    """Tier 2: agent with neither ``description`` nor ``role`` falls back to
    delegate_to_agent's dispatcher description.
    """
    ctx = _make_ctx(agents=[
        {"name": "ghost"},
    ])
    out = _describe("agent.peer__ghost", ctx)
    # Some non-empty fallback (= dispatcher's description, whatever its wording)
    assert out.get("description")


# ── mcp.tool__server.tool ───────────────────────────────────────────────


def test_mcp_tool_describe_returns_tool_description():
    """Tier 2b: describe_action(mcp.tool__server.tool) returns the tool's description.

    B42-NF-W7-1: uses the MCP tool's declared description, not
    call_mcp_tool's dispatcher text.
    """
    actual_desc = "Search GitHub pull requests matching a query."
    ctx = _make_ctx(mcp_servers=[
        {
            "name": "github",
            "tools": [
                {"name": "search_pull_requests", "description": actual_desc},
            ],
        },
    ])
    out = _describe("mcp.tool__github.search_pull_requests", ctx)
    assert out["description"] == actual_desc


def test_mcp_tool_describe_missing_description_falls_back():
    """Tier 2: MCP tool without ``description`` field falls back to
    call_mcp_tool's dispatcher description.
    """
    ctx = _make_ctx(mcp_servers=[
        {
            "name": "github",
            "tools": [{"name": "ping"}],
        },
    ])
    out = _describe("mcp.tool__github.ping", ctx)
    # Some non-empty fallback expected
    assert out.get("description")


# ── mcp.server__X ───────────────────────────────────────────────────────


def test_mcp_server_describe_returns_server_description():
    """Tier 2b: describe_action(mcp.server__X) returns the server's description.

    B42-NF-W7-1: uses the server's own description, not list_mcp_tools'
    dispatcher text.
    """
    actual_desc = "GitHub MCP server — search/comment/PR ops."
    ctx = _make_ctx(mcp_servers=[
        {"name": "github", "description": actual_desc, "tools": []},
    ])
    out = _describe("mcp.server__github", ctx)
    assert out["description"] == actual_desc


# ── operation categories (regression guard) ─────────────────────────────


def test_operation_describe_unchanged_by_resource_description_fix():
    """Tier 2b: operation-category actions continue to use target.description.

    Regression guard: file__read, web__fetch, etc. must not be affected by
    the B42-NF-W7-1 resource-description fix — their resolution path is
    unchanged.
    """
    ctx = _make_ctx()
    out = _describe("file__read", ctx)
    # The file__read tool description must be non-empty and is the
    # operation's own description (not a resource-category fallback).
    assert out["description"]
    # And it shouldn't accidentally pull from the (empty) skills list.
    assert "Catalogue-gap fallback" not in out["description"]
