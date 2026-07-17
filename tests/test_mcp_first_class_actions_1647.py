"""Tier 2: #1647/#3026 — ``mcp__<server>__<tool>`` RESOLVES but is not enumerated.

#1647 made each connected MCP tool a first-class action: dispatchable by name AND
enumerated into the LLM's tools= payload. #3026 kept the first half and removed
the second, because enumeration is what made the payload scale with the
operator's MCP surface (one tool per MCP tool). What this file pins now:

  - DISPATCH (#1647, KEPT): invoke_action("mcp__<server>__<tool>", args={tool
    params}) resolves to the EXISTING mcp_call_tool verb with the IDENTICAL
    {tool, tool_args} shape mcp__call_tool produces → same permission gate
    (security: no new dispatch path). Kept because a pipeline DSL ``tool:`` step
    may name an MCP tool directly (``tool: mcp__echo__ping``) — an author-time
    name, which costs zero payload to resolve.
  - NOT ENUMERATED (#3026): the mcp category lists its static verbs only. The
    payload consequence is pinned in test_resource_collapse_invariant_3026.py.
  - DESCRIBE (#3026): a non-enumerated name describes as its routing target
    (mcp_call_tool). #1647 surfaced the tool's own inputSchema here; that need is
    served by ``mcp__list_tools``, whose result ships each tool's ``inputSchema``
    VERBATIM — #879 built it that way explicitly so no extra round-trip is
    needed (see tools/mcp.py). #1647 did not check, and re-added enumeration for
    a gap #879 had already closed.

Static mcp verbs (mcp__call_tool / mcp__list_tools / …) match _OPERATION_RULES
first and are unaffected throughout.

Real ToolContext + RouterCallerState + registry (no mocks).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    _describe_one,
    _enumerate_category,
    _handle_invoke_action,
)
from reyn.tools.universal_dispatch import resolve_invoke_action

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


class _FakeEvents:
    def emit(self, *a, **k) -> None:
        pass


def _ctx(with_tools: bool = True) -> ToolContext:
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            mcp_servers=_MCP_SERVERS if with_tools else None,
        ),
    )


def test_per_tool_dispatch_matches_call_tool_gate() -> None:
    """Tier 2: #1647 (security) — mcp__<server>__<tool> resolves to the SAME
    mcp_call_tool verb with the IDENTICAL {tool, tool_args} shape that the generic
    mcp__call_tool produces, so the dispatch routes through the existing permission
    gate unchanged (no bypass). The per-tool action only moves the server__tool id
    into the NAME and the tool params into one args level."""
    per_tool = resolve_invoke_action("mcp__brave__search", {"query": "x"})
    generic = resolve_invoke_action(
        "mcp__call_tool", {"tool": "brave__search", "tool_args": {"query": "x"}},
    )
    assert per_tool.target_tool_name == "mcp_call_tool"
    assert per_tool.target_tool_name == generic.target_tool_name
    assert per_tool.target_args == {"tool": "brave__search", "tool_args": {"query": "x"}}
    assert per_tool.target_args == generic.target_args


def test_static_mcp_verb_unaffected() -> None:
    """Tier 2: #1647 — a static mcp verb (entry without "__") still matches
    _OPERATION_RULES first; the per-tool _RESOURCE_RULES["mcp"] only catches
    dynamic <server>__<tool> names."""
    r = resolve_invoke_action("mcp__list_tools", {"server": "brave"})
    assert r.target_tool_name == "list_mcp_tools"


def test_enumeration_omits_per_tool_actions_even_with_warm_cache() -> None:
    """Tier 2: #3026 — the mcp category enumerates its static verbs ONLY, even when
    the FP-0037 snapshot is warm and carries tools. This is the #1647 reversal: a
    connected MCP tool must not become an entry in the LLM's tools= payload, or the
    payload scales with the operator's MCP surface. The tool stays reachable via
    mcp__list_tools + mcp__call_tool (and by name from a pipeline DSL step)."""
    names = {it["qualified_name"] for it in _enumerate_category("mcp", _ctx())}
    assert not any(n.startswith("mcp__brave__") for n in names), (
        "per-tool MCP actions must not be enumerated (#3026)"
    )
    assert "mcp__call_tool" in names, "static verbs still present"
    assert "mcp__list_tools" in names, "the discovery verb is how tools are found"


def test_enumeration_identical_whether_cache_warm_or_cold() -> None:
    """Tier 2: #3026 — enumeration does not consult the tool cache at all, so a warm
    cache and a cold one produce the SAME action set. Pins the invariant at its
    source: what the LLM is shown is independent of session-discovered resources."""
    warm = {it["qualified_name"] for it in _enumerate_category("mcp", _ctx())}
    cold = {
        it["qualified_name"]
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
async def test_per_tool_dispatch_reaches_mcp_gate_e2e() -> None:
    """Tier 2: #1647 (security, e2e) — a per-tool invoke_action drives the FULL
    real dispatch chain (resolve → mcp_call_tool verb → split → _handle_call_mcp_tool
    → host.mcp_call_tool) and reaches the SAME gated boundary the generic
    mcp__call_tool uses, with the server/tool/args intact — proving the new
    resolution path does NOT bypass the gate (it routes through it identically)."""
    host = _RecordingHost()
    ctx = ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(host=host, mcp_servers=_MCP_SERVERS),
    )
    await _handle_invoke_action(
        {"action_name": "mcp__brave__search", "args": {"query": "hello"}}, ctx,
    )
    # The per-tool action's params reached host.mcp_call_tool as
    # (server, tool, the tool's own args) — one level, no nesting/collision.
    assert host.calls == [("brave", "search", {"query": "hello"})]


def test_describe_per_tool_name_returns_routing_target_schema() -> None:
    """Tier 2: #3026 — a per-tool name still DESCRIBES (it resolves), but as its
    routing target ``mcp_call_tool`` rather than via a per-resource schema override.
    #1647 surfaced the tool's own inputSchema here; #3026 removed that override
    along with the enumeration it existed to enrich. The tool's real schema is not
    lost — ``mcp__list_tools`` returns each tool's ``inputSchema`` verbatim, which is
    the surface #879 built for exactly this and #1647 overlooked."""
    one = _describe_one("mcp__brave__search", _ctx(), get_default_registry())
    assert one is not None
    props = one["input_schema"].get("properties") or {}
    assert "tool" in props and "tool_args" in props, (
        "describes as the mcp_call_tool verb it routes to"
    )


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
        events=_FakeEvents(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=RouterCallerState(host=_Host()),
    )
    result = asyncio.run(LIST_MCP_TOOLS.handler({"server": "brave"}, ctx))
    entry = result["mcp_tools"][0]
    assert entry["inputSchema"] == _TOOL_SCHEMA, (
        "the tool's own schema reaches the caller verbatim — the #1647 need, "
        "already served by #879"
    )
    assert entry["name"] == "brave__search"
