"""Tier 2: #1647/#3026/#3429 — an MCP tool is an ARGUMENT, never an action.

#1647 made each connected MCP tool a first-class action: dispatchable by an
``mcp__<server>__<tool>`` name AND enumerated into the LLM's tools= payload.
#3026 removed the enumeration, because that is what made the payload scale with
the operator's MCP surface. #3429 removed the remaining half — the name — with
the rest of the ``<category>__<verb>`` spelling: an MCP tool is reached by
passing its ``<server>__<tool>`` identifier as ``mcp_call_tool``'s ``tool``
ARGUMENT, which is the shape ``list_mcp_tools`` already returns. What this file
pins now:

  - REACHABILITY (#1647's real requirement, KEPT): the tool is callable with the
    tool's own params, through the EXISTING ``mcp_call_tool`` verb and therefore
    the same permission gate — no second dispatch path, from chat or from a
    pipeline DSL ``tool:`` step.
  - NOT ENUMERATED (#3026): the mcp category lists its static verbs only. The
    payload consequence is pinned in test_resource_collapse_invariant_3026.py.
  - SCHEMA (#3026): the tool's own ``inputSchema`` is served by
    ``list_mcp_tools``, whose result ships it VERBATIM — #879 built it that way
    explicitly so no extra round-trip is needed (see tools/mcp.py). #1647 did
    not check, and re-added enumeration for a gap #879 had already closed.

Real ToolContext + RouterCallerState + registry (no mocks).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    _describe_one,
    _enumerate_category,
    _handle_invoke_action,
)
from reyn.tools.universal_dispatch import is_known_action

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}
_MCP_SERVERS = [
    {
        "name": "brave",
        "description": "Brave search MCP server",
        "tools": [
            {"name": "search", "description": "Run a web search", "inputSchema": _TOOL_SCHEMA},
        ],
    },
]


def _ctx(with_tools: bool = True) -> ToolContext:
    return ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            mcp_servers=_MCP_SERVERS if with_tools else None,
        ),
    )


def test_per_tool_name_is_not_an_action() -> None:
    """Tier 2: #3429 — an ``mcp__<server>__<tool>`` name is not a catalog action.

    It was the last author-time survivor of the qualified spelling. A model or a
    pipeline author naming it now gets the ordinary unknown-action error with
    suggestions, not a second dispatch route."""
    assert not is_known_action("mcp__brave__search")
    assert is_known_action("mcp_call_tool")


def test_enumeration_omits_per_tool_actions_even_with_warm_cache() -> None:
    """Tier 2: #3026 — the mcp category enumerates its static verbs ONLY, even when
    the FP-0037 snapshot is warm and carries tools. This is the #1647 reversal: a
    connected MCP tool must not become an entry in the LLM's tools= payload, or the
    payload scales with the operator's MCP surface. The tool stays reachable via
    list_mcp_tools + mcp_call_tool (and by name from a pipeline DSL step)."""
    names = {it["action_name"] for it in _enumerate_category("mcp", _ctx())}
    assert not any(n.startswith("mcp__brave__") for n in names), (
        "per-tool MCP actions must not be enumerated (#3026)"
    )
    assert "mcp_call_tool" in names, "static verbs still present"
    assert "list_mcp_tools" in names, "the discovery verb is how tools are found"


def test_enumeration_identical_whether_cache_warm_or_cold() -> None:
    """Tier 2: #3026 — enumeration does not consult the tool cache at all, so a warm
    cache and a cold one produce the SAME action set. Pins the invariant at its
    source: what the LLM is shown is independent of session-discovered resources."""
    warm = {it["action_name"] for it in _enumerate_category("mcp", _ctx())}
    cold = {
        it["action_name"]
        for it in _enumerate_category("mcp", _ctx(with_tools=False))
    }
    assert warm == cold


class _RecordingHost:
    """Minimal router host that records the MCP call reaching the gate boundary
    (host.mcp_call_tool is what _handle_call_mcp_tool delegates to on the router
    path, downstream of which lives the permission gate)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        self.calls.append((server, tool, dict(args)))
        return {"ok": True, "result": "recorded"}


@pytest.mark.asyncio
async def test_per_tool_call_reaches_mcp_gate_e2e() -> None:
    """Tier 2: #1647's requirement (security, e2e) — calling a specific MCP tool
    drives the FULL real dispatch chain (invoke_action → mcp_call_tool verb →
    split → _handle_call_mcp_tool → host.mcp_call_tool) and reaches the gated
    boundary with server/tool/args intact. #3429: the tool identifier rides in
    ``tool``, so there is ONE route to that boundary rather than two."""
    host = _RecordingHost()
    ctx = ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(host=host, mcp_servers=_MCP_SERVERS),
    )
    await _handle_invoke_action(
        {
            "action_name": "mcp_call_tool",
            "args": {"tool": "brave__search", "tool_args": {"query": "hello"}},
        },
        ctx,
    )
    # The call reached host.mcp_call_tool as
    # (server, tool, the tool's own args) — one level, no nesting/collision.
    assert host.calls == [("brave", "search", {"query": "hello"})]


def test_describe_mcp_call_tool_shows_the_generic_envelope() -> None:
    """Tier 2: #3026/#3429 — ``describe_action`` on the reachable name describes
    ``mcp_call_tool``'s ``{tool, tool_args}`` envelope, not a per-resource schema
    override. #1647 surfaced the tool's own inputSchema through a per-tool action;
    #3026 removed that override with the enumeration it enriched, and #3429
    removed the name. The tool's real schema is not lost — ``list_mcp_tools``
    returns each tool's ``inputSchema`` verbatim, the surface #879 built for
    exactly this and #1647 overlooked (pinned by the next test)."""
    one = _describe_one("mcp_call_tool", _ctx(), get_default_registry())
    assert one is not None
    props = one["input_schema"].get("properties") or {}
    assert "tool" in props and "tool_args" in props


def test_list_mcp_tools_result_carries_each_tools_real_input_schema() -> None:
    """Tier 2: #3026 — the load-bearing claim behind removing the per-tool describe
    override: ``list_mcp_tools`` ships each tool's REAL ``inputSchema``, so a caller
    gets the same schema #1647 enumerated per-tool, in one call and zero payload.

    Drives the real handler against a host returning an MCP-shaped listing; asserts
    the tool's declared schema survives to the caller (not a summary of it)."""
    from reyn.tools.mcp import LIST_MCP_TOOLS

    class _Host:
        async def mcp_list_tools(self, server: str) -> list[dict]:
            return [{"name": "search", "description": "Run a web search",
                     "inputSchema": _TOOL_SCHEMA}]

    ctx = ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=RouterCallerState(host=_Host()),
    )
    result = asyncio.run(LIST_MCP_TOOLS.handler({"server": "brave"}, ctx))
    entry = result["mcp_tools"][0]
    assert entry["inputSchema"] == _TOOL_SCHEMA, (
        "the tool's own schema reaches the caller verbatim — the #1647 need, "
        "already served by #879"
    )
    assert entry["name"] == "brave__search"
