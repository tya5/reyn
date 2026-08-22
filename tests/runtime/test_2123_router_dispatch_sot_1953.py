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
    # delegate_to_agent retired, proposal 0067 P6 (#3978) — send_to_session/
    # run_prompt reach another agent's context the same way.
    "list_agents", "describe_agent",
    "spawn_session", "spawn_agent", "create_topology",
    # Proposal 0067 P5 (#3978): send_to_session gained a router-dispatched
    # ToolDefinition (fire-and-forget delivery primitive).
    "send_to_session",
    # Proposal 0067 P4d (#3978): run_prompt gained a router-dispatched
    # ToolDefinition (sync run+collect against a live peer, collect="attached").
    "run_prompt",
    "reyn_repo_list", "reyn_repo_read",
    "web_search", "web_fetch",
    "read_file", "write_file", "delete_file", "list_directory",
    "edit_file", "glob_files", "grep_files",
    "list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool",
    # #2597 slice ②a: resources consumption — parallel to the tools surface above.
    "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    # #2597 slice ②b: resource subscriptions — the async push event-source.
    "subscribe_mcp_resource", "unsubscribe_mcp_resource",
    # #4686: list_mcp_subscriptions — the read-back for the async push
    # source above (per-connection tracked/honored state), router_dispatched
    # so chat + pipeline both reach it, mirroring list_mcp_servers's own
    # no-args discovery shape.
    "list_mcp_subscriptions",
    # #2597 slice ②c: prompts consumption — parallel to the resources surface above.
    "list_mcp_prompts", "get_mcp_prompt",
    "remember_shared", "remember_agent", "forget_memory", "list_memory", "read_memory_body",
    "compact",
    # FP-0057 Phase 1: embed gained a router-dispatched ToolDefinition (the raw
    # user-facing embedding primitive; default-allow) so chat + pipeline reach
    # the new embed op handler.
    #
    # FP-0066 P1b: semantic_search / drop_source / index_update — the
    # agent-facing layer-1 in-core RAG tools (FP-0057 Phase 2a / #3222) — are
    # RETIRED, so their router_dispatched membership is removed from this
    # golden along with the tools.
    "embed",
    # #2692 (part of the #2688 sweep): present + render_template gained a ToolDefinition
    # (router_dispatched=True) so chat + pipeline can reach the existing op handlers.
    "present", "render_template",
    "list_actions", "search_actions", "describe_action", "invoke_action",
    # FP-0066 P0 (#3247): load_skill gained a router-dispatched ToolDefinition
    # (the dedicated skill-activation verb, extracted out of the former
    # file-read SKILL.md special-case) so chat + pipeline reach the new
    # load_skill op handler.
    "load_skill",
    # FP-0066 P3c (#3247 firm §3): search_knowledge gained a router-dispatched
    # ToolDefinition (the new `knowledge` category's semantic-search verb,
    # qualified `search_knowledge`) so chat + pipeline reach the new
    # search_knowledge handler.
    "search_knowledge",
    # #3429: the 23 catalog actions that had NO router_dispatched flag because
    # they were reached only through ``invoke_action`` — ``_invoke_router_tool``
    # re-routed anything containing ``__`` (their QUALIFIED spelling) to the
    # wrapper, so the flat name never needed a dispatch route. With one name per
    # action that arm is gone, and an ADVERTISED action without the flag lands on
    # the "unhandled tool" safety return — the exact drift class this file's
    # cross-seam guard exists to catch, now covering them too.
    "exec",
    "install_plugin", "uninstall_plugin", "list_plugins",
    "mcp_call_tool", "mcp_drop_server",
    "mcp_install_local", "mcp_install_package", "mcp_install_registry",
    "mcp_search_registry",
    "pipeline_install_local", "pipeline_install_source",
    "pipeline_list",
    # Proposal 0067 P7 (#3978): run_pipeline_async / run_pipeline_inline /
    # run_pipeline_inline_async unified into run_pipeline (4 names -> 1, 0
    # aliases) — collect=/name=/definition= select the former verbs.
    "run_pipeline",
    # proposal 0067 P4 (#3978): describe_task / list_tasks / cancel_task —
    # router_dispatched=True, dispatched via invoke_action like the catalog
    # actions above.
    "describe_task", "list_tasks", "cancel_task",
    "presentation_install_local",
    "reyn_repo_glob", "reyn_repo_grep",
    "skill_install_local", "skill_install_source", "skill_list",
    # #3465: emit_hook_event / hooks_add gained router_dispatched=True when
    # they were wired into the catalog action-membership table
    # (universal_dispatch._CATEGORY_ACTIONS["hooks"]) — dispatched via
    # invoke_action, same requirement #3429 states above for the 23 catalog
    # actions.
    "emit_hook_event", "hooks_add",
    # #5012-A PR #5038: describe_session gained a router-dispatched
    # ToolDefinition (read-only session introspection: write scope / own
    # position / auth status) so chat + pipeline reach the new
    # describe_session op handler.
    "describe_session",
})

_AG = [{"name": "a1", "description": "d"}]
_MCP = [{"name": "fs", "description": "Filesystem MCP server"}]


def _advertised_bare_router_tools() -> "set[str]":
    """Router-tool names build_tools advertises across the broadest surface (all
    gates open, wrappers both ways) — what the LLM can actually call."""
    names: set[str] = set()
    for wrappers in (True, False):
        tools = build_tools(
            _AG,
            file_permissions={"read": ["src"], "write": ["out"]},
            mcp_servers=_MCP, universal_wrappers_enabled=wrappers, compact_visible=True,
        )
        names |= {t["function"]["name"] for t in tools}
    # #3429: the filter that used to sit here dropped ``__``-bearing names,
    # because a qualified alias routed via invoke_action rather than through the
    # dispatch set. There is no such name any more, so every advertised name is
    # in scope for the cross-seam guard below.
    return names


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


def test_advertised_router_tool_implies_dispatched():
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
