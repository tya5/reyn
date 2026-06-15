"""Tier 3a: #1646 full-flow threading — MCP tool args reach the boundary non-empty.

The load-bearing gate the isolated schema tests miss: drive the REAL universal-scheme
wrapped path. The live MCP call is invoke_action(action_name="mcp__call_tool",
args={tool:"<server>__<tool>", tool_args:{...}}) → resolve_invoke_action → mcp_verbs
`_handle_mcp_call_tool` (splits the `<server>__<tool>` id, reads the renamed inner
`tool_args`) → DELEGATES to mcp.py `_handle_call_mcp_tool` (also reads `tool_args`) →
host.mcp_call_tool(server, tool, <params>). Assert the params arrive there NON-EMPTY with
a NON-DEFAULT value (so a zeroing/unwired path can't pass silently).

Pre-#1646 the inner key was `args` at BOTH mcp_verbs (LLM-facing) and the delegation,
colliding with invoke_action's outer `args` → the LLM collapsed it (params flat, inner
dropped) → empty at the MCP call (owner-observed). The distinct `tool_args` removes the
collision AND keeps the cross-file (mcp_verbs→mcp.py) delegation key consistent.

Real handlers + real ToolContext + a real Fake host (records the boundary call) — no
mocks (mirrors test_describe_mcp_tool_handler).
"""
from __future__ import annotations

import asyncio

from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext

_NON_DEFAULT = "REYN_1646_NONDEFAULT_a7f3"  # distinctive → no silent default-pass


class _RecordingMCPHost:
    """A trivial echo MCP tool at the host.mcp_call_tool boundary — records what it gets."""

    def __init__(self) -> None:
        self.mcp_calls: list[dict] = []

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        self.mcp_calls.append({"server": server, "tool": tool, "args": dict(args)})
        return {"status": "ok", "server": server, "tool": tool, "echo": dict(args)}


def _ctx(host: _RecordingMCPHost) -> ToolContext:
    return ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=RouterCallerState(host=host),
    )


def _invoke_action(inner: dict, host: _RecordingMCPHost) -> dict:
    handler = get_default_registry().lookup("invoke_action").handler
    return asyncio.run(
        handler({"action_name": "mcp__call_tool", "args": inner}, _ctx(host))
    )


def test_mcp_tool_args_reach_boundary_via_invoke_action():
    """Tier 3a: #1646 — correctly-nested tool_args thread invoke_action → mcp_verbs →
    mcp.py → host.mcp_call_tool with the NON-DEFAULT value intact (the renamed contract
    + the cross-file delegation work end-to-end on the REAL wrapped path)."""
    host = _RecordingMCPHost()
    # The shape the renamed mcp_verbs schema asks for (the invoke_action-wrapped path):
    # outer invoke_action args = {tool:"<server>__<tool>", tool_args:{<params>}}.
    inner = {"tool": "web-search__search", "tool_args": {"query": _NON_DEFAULT}}
    result = _invoke_action(inner, host)

    assert "error" not in result, f"invoke_action returned error: {result}"
    assert host.mcp_calls, "MCP boundary (host.mcp_call_tool) was never reached"
    rec = host.mcp_calls[0]
    # The target tool's params arrived non-empty, with the NON-DEFAULT value intact.
    assert rec["args"] == {"query": _NON_DEFAULT}
    assert rec["server"] == "web-search"
    assert rec["tool"] == "search"


def test_mcp_pre_fix_single_nest_would_drop_args():
    """Tier 3a: #1646 contrast — the PRE-fix collapse (params flat in invoke_action's
    args, NO inner tool_args level) yields EMPTY args at the MCP boundary = the
    owner-observed failure. Documents WHY the distinct inner key matters."""
    host = _RecordingMCPHost()
    collapsed = {"tool": "web-search__search", "query": _NON_DEFAULT}  # no inner tool_args
    result = _invoke_action(collapsed, host)
    assert "error" not in result, f"invoke_action returned error: {result}"
    assert host.mcp_calls, "MCP boundary was never reached"
    # No inner tool_args ⇒ the tool runs with EMPTY args (the bug); query did NOT arrive.
    assert host.mcp_calls[0]["args"] == {}
