"""Tier 2: #2123 — REGISTRY_DISPATCH_TOOLS derived from the router_dispatched SoT.

The router-only-tool 3-seam wiring (register → advertise → dispatch) recurrently drifted
(#2120 advertise-miss, #2122 dispatch-miss, read_tool_result advertised-but-unhandled).
This refactor makes the dispatch seam DERIVED from a single per-tool flag
(`ToolDefinition.router_dispatched`) and adds the feasible cross-seam guard
(advertised ⟹ dispatched), so a new router-only tool is dispatch-wired by one flag and
the drift class is caught structurally.

These tests are the review gates:
- migration-equivalence (no behavior change): the derived set == the old hand-maintained
  frozenset MINUS the one documented dead-drift removal (`read_tool_result`, #1449).
- the cross-seam guard: every advertised bare router tool is dispatch-routed.
"""
from __future__ import annotations

from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.router_tools import build_tools
from reyn.tools import get_default_registry

# The pre-#2123 REGISTRY_DISPATCH_TOOLS membership (the hand-maintained frozenset) MINUS
# `read_tool_result` (#1449 retired dead drift: unregistered + unadvertised → unreachable;
# removed as zero-behavior-change cleanup). This golden is the no-behavior-change oracle.
#
# NOTE: this golden also DOUBLES as a deliberate dispatch-membership gate. A change to any
# tool's ``router_dispatched`` flag flips the derived set, so this test goes RED — that is
# the gate WORKING, not a break: update this golden INTENTIONALLY (with the membership
# change) when adding/removing a dispatch-routed tool, the same way #1822 / #2111 / #1056
# require deliberate updates to their exhaustiveness lists.
_EXPECTED_DISPATCH: "frozenset[str]" = frozenset({
    "list_agents", "describe_agent", "delegate_to_agent",
    "session_spawn", "agent_spawn", "topology_create",
    "reyn_src_list", "reyn_src_read",
    "web_search", "web_fetch",
    "read_file", "write_file", "delete_file", "list_directory",
    "edit_file", "glob_files", "grep_files",
    "list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool",
    # #2597 slice ②a: resources consumption — parallel to the tools surface above.
    "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    "remember_shared", "remember_agent", "forget_memory", "list_memory", "read_memory_body",
    "recall", "drop_source", "compact",
    "list_actions", "search_actions", "describe_action", "invoke_action",
})

_AG = [{"name": "a1", "description": "d"}]
_MCP = [{"name": "fs", "description": "Filesystem MCP server"}]


def _advertised_bare_router_tools() -> "set[str]":
    """Bare (non-qualified) router-tool names build_tools advertises across the broadest
    surface (all gates open, wrappers both ways) — what the LLM can actually call."""
    names: set[str] = set()
    for wrappers in (True, False):
        tools = build_tools(
            _AG,
            file_permissions={"read": ["src"], "write": ["out"]},
            mcp_servers=_MCP, universal_wrappers_enabled=wrappers, compact_visible=True,
        )
        names |= {t["function"]["name"] for t in tools}
    return {n for n in names if "__" not in n}  # qualified aliases route via invoke_action


def test_dispatch_set_is_migration_equivalent():
    """Tier 2: (no-behavior-change oracle) the DERIVED REGISTRY_DISPATCH_TOOLS equals the
    pre-refactor hand-maintained set minus the one documented dead-drift removal
    (read_tool_result). RED if the derivation adds/drops any tool vs the frozen baseline."""
    assert RouterLoop.REGISTRY_DISPATCH_TOOLS == _EXPECTED_DISPATCH


def test_dispatch_set_derives_from_router_dispatched_flag():
    """Tier 2: the set IS the per-tool router_dispatched SoT — it equals exactly the
    registry tools carrying the flag (no hand-maintained drift from the markers)."""
    reg = get_default_registry()
    from_flag = {
        d.name for d in (reg.lookup(n) for n in reg.names())
        if d is not None and d.router_dispatched
    }
    assert RouterLoop.REGISTRY_DISPATCH_TOOLS == from_flag


def test_read_tool_result_dead_drift_removed():
    """Tier 2: read_tool_result (#1449 retired) is no longer in the dispatch set — the
    Q2 dead-drift resolution. RED if it ever re-enters (a non-registry name can't carry
    the flag, so the derivation excludes it by construction)."""
    assert "read_tool_result" not in RouterLoop.REGISTRY_DISPATCH_TOOLS


def test_advertised_bare_router_tool_implies_dispatched():
    """Tier 2: (THE cross-seam guard — the recurrence-killer) every bare router tool that
    build_tools ADVERTISES is in REGISTRY_DISPATCH_TOOLS, so the LLM can never call an
    advertised tool that falls through to 'unhandled tool' (#2120 / read_tool_result
    class). Introspects build_tools OUTPUT → condition-agnostic (covers all advertise
    blocks). RED if a tool is advertised but not dispatch-routed."""
    advertised = _advertised_bare_router_tools()
    undispatched = sorted(advertised - RouterLoop.REGISTRY_DISPATCH_TOOLS)
    assert not undispatched, (
        f"advertised but NOT dispatch-routed (would hit 'unhandled tool'): {undispatched}"
    )


def test_every_dispatch_name_is_a_registry_tool_with_flag():
    """Tier 2: (Q3 derivation integrity) every dispatch name resolves to a registry
    ToolDefinition carrying router_dispatched=True — no hardcoded/non-registry residual."""
    reg = get_default_registry()
    for name in RouterLoop.REGISTRY_DISPATCH_TOOLS:
        d = reg.lookup(name)
        assert d is not None, f"dispatch name {name!r} is not a registry ToolDefinition"
        assert d.router_dispatched, f"{name!r} dispatched but router_dispatched is False"
