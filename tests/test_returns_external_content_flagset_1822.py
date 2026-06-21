"""Tier 2: returns_external_content flag-set completeness (FP-0050 / #1822 S2).

The flag-set IS the security gate (a missed external tool = an unfenced injection
vector — the same implicit-miss class as the dead-EP1 catch). This pin is
**exhaustive**: every registered ToolDefinition must be in exactly-one of
{_EXTERNAL (fenced), _NOT_EXTERNAL (documented not-fenced)}. A new tool — or a
flag flip — that isn't classified here fails the test, so trust classification is
**completeness-by-construction**, never a silent unflagged-default.

Real default registry, no mocks. See FP-0050 §2/§6 for the threat model + the
deferred file-read / exec-output (scan-only in S2, tracked fast-follow).
"""
from __future__ import annotations

import pytest

from reyn.tools import get_default_registry

# Fenced (returns_external_content=True): content from outside the trust boundary
# — external network / external store / user-written disk.
_EXTERNAL = {
    "list_memory", "read_memory_body",   # user/agent-written .md
    "recall",                            # RAG over user content (memory/docs/chat)
    "call_mcp_tool", "mcp_call_tool",    # external MCP server result
    "list_mcp_tools", "describe_mcp_tool",  # external server-authored descriptions
    "mcp_search_registry",               # external registry listing
    "web_search", "web_fetch",           # internet
}

# Not fenced (returns_external_content=False): each justified below. Scan-all
# still runs on these at the chokepoint (detection completeness).
_NOT_EXTERNAL = {
    # — deferred to the tracked fast-follow (scan-only in S2; FP-0050 §6) —
    # file content / exec output: agent work-products, secondary vector; fencing
    # every such result = broad bloat at low precision → content-origin follow-up.
    "read_file", "grep_files", "glob_files", "list_directory", "sandboxed_exec",
    # — principal / peer (lead finding: explicitly classified) —
    # ask_user: the user is the trust ROOT — their input is the legitimate
    # instruction channel, not untrusted-data (fencing it would break the
    # user-message-as-instruction model). User-relayed paste is out of the S2
    # threat model (principal's own channel).
    "ask_user",
    # delegate_to_agent: async dispatch → returns a "spawned" ACK, not the
    # sub-agent's output. The peer reply arrives via the A3 inbound seam
    # (EP5, handle_agent_response → history) and is fenced there in S4.
    "delegate_to_agent",
    # — writes / installs / deletes: return status, not external content —
    "write_file", "edit_file", "delete_file", "drop_source",
    "remember_shared", "remember_agent", "forget_memory",
    "mcp_install", "mcp_install_local", "mcp_install_package",
    "mcp_install_registry", "mcp_drop_server",
    "cron_register", "cron_unregister", "cron_enable", "cron_disable",
    "lint",
    # — catalog / discovery (reyn-assembled or operator config) —
    "list_skills", "describe_skill", "list_agents", "describe_agent",
    "list_actions", "search_actions", "describe_action",
    "list_mcp_servers", "cron_list",
    # — control / orchestration —
    # decompose (#1953): task-driven analog of plan; the synthesized reply is
    # reyn-assembled from sub-task results, and any external content a sub-task
    # reads is fenced at that sub-run's own tool-result chokepoint (like plan).
    "compact", "plan", "decompose",
    # invoke_action: generic dispatcher — trust resolved by the EFFECTIVE inner
    # name at dispatch() (the dispatch-tag), not by this wrapper.
    "invoke_action",
    # invoke_skill: reyn-produced output; external content the sub-skill read is
    # fenced at the sub-run's own tool-result chokepoint (recursive).
    "invoke_skill",
    # — reyn's own framework source (trusted) —
    "reyn_src_list", "reyn_src_read", "reyn_src_glob", "reyn_src_grep",
}


def test_classification_is_exhaustive():
    """Tier 2: every registered tool is classified in exactly one list.

    Completeness-by-construction: a new/missed tool (silent unflagged-default) or
    a stale entry fails here, forcing explicit trust classification.
    """
    registered = set(get_default_registry().names())
    documented = _EXTERNAL | _NOT_EXTERNAL

    unclassified = registered - documented
    assert not unclassified, (
        "unclassified tool(s) — add to _EXTERNAL or _NOT_EXTERNAL with a reason: "
        f"{sorted(unclassified)}"
    )
    stale = documented - registered
    assert not stale, f"classified tool(s) no longer registered — remove: {sorted(stale)}"
    overlap = _EXTERNAL & _NOT_EXTERNAL
    assert not overlap, f"tool(s) in BOTH lists: {sorted(overlap)}"


@pytest.mark.parametrize("name", sorted(_EXTERNAL))
def test_external_source_tools_flagged(name):
    """Tier 2: every clear-external tool sets returns_external_content=True."""
    td = get_default_registry().lookup(name)
    assert td is not None, f"{name} not registered"
    assert td.returns_external_content is True, f"{name} must be flagged external"


@pytest.mark.parametrize("name", sorted(_NOT_EXTERNAL))
def test_not_external_tools_unflagged(name):
    """Tier 2: trusted-internal / deferred tools are NOT fenced (scan-only)."""
    td = get_default_registry().lookup(name)
    assert td is not None, f"{name} not registered"
    assert td.returns_external_content is False, f"{name} must not be flagged external"
