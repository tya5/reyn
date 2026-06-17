"""Tier 2: #1647 — MCP tools as first-class qualified actions mcp__<server>__<tool>.

FP-0034: each connected MCP tool is a first-class action selectable by name with
its REAL inputSchema (like skill__<name>), retiring the generic call_mcp_tool
double-args foot-gun (#1646). This pins the three surfaces:

  - DISPATCH: invoke_action("mcp__<server>__<tool>", args={tool params}) resolves
    to the EXISTING mcp_call_tool verb with the IDENTICAL {tool, tool_args} shape
    mcp__call_tool produces → same permission gate (security: no new dispatch path).
  - ENUMERATE: the mcp category lists mcp__<server>__<tool> per cached tool
    (from router_state.mcp_servers[*].tools) alongside the static verbs.
  - DESCRIBE: describe surfaces the tool's OWN inputSchema (one args level), not
    the generic call_mcp_tool {tool, tool_args} envelope.

Static mcp verbs (mcp__call_tool / mcp__list_tools / …) are unaffected — they
match _OPERATION_RULES first.

Real ToolContext + RouterCallerState + registry (no mocks).
"""
from __future__ import annotations

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
            available_skills=[],
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


def test_enumeration_lists_per_tool_actions() -> None:
    """Tier 2: #1647 — the mcp category enumerates mcp__<server>__<tool> for each
    cached tool ALONGSIDE the static verbs (read of the FP-0037 snapshot, no probe)."""
    names = {it["qualified_name"] for it in _enumerate_category("mcp", _ctx())}
    assert "mcp__brave__search" in names, "per-tool action enumerated"
    assert "mcp__call_tool" in names, "static verbs still present"


def test_enumeration_empty_when_cache_cold() -> None:
    """Tier 2: #1647 — no warm cache (mcp_servers None) → only the static verbs,
    no per-tool entries (graceful)."""
    names = {it["qualified_name"] for it in _enumerate_category("mcp", _ctx(with_tools=False))}
    assert not any(n.startswith("mcp__brave__") for n in names)
    assert "mcp__call_tool" in names


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


def test_describe_surfaces_tool_input_schema() -> None:
    """Tier 2: #1647 — describe_action on a per-tool action returns the MCP tool's
    OWN inputSchema (one args level), NOT the generic call_mcp_tool {tool, tool_args}
    envelope. This is the #1646 foot-gun fix."""
    one = _describe_one("mcp__brave__search", _ctx(), get_default_registry())
    assert one is not None
    assert one["input_schema"] == _TOOL_SCHEMA
    assert one["description"] == "Run a web search"
    # NOT the generic envelope keys
    assert "tool_args" not in (one["input_schema"].get("properties") or {})
