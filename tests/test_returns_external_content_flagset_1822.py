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
    # #2597 slice ②a: resources consumption — same fencing rationale as the tools
    # surface above (external server-authored listing / content).
    "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    # #2597 slice ②c: prompts consumption — same fencing rationale as the
    # resources surface above (external server-authored listing / content).
    "list_mcp_prompts", "get_mcp_prompt",
    "web_search", "web_fetch",           # internet
}

# Not fenced (returns_external_content=False): each justified below. Scan-all
# still runs on these at the chokepoint (detection completeness).
_NOT_EXTERNAL = {
    # — deferred to the tracked fast-follow (scan-only in S2; FP-0050 §6) —
    # file content / exec output: agent work-products, secondary vector; fencing
    # every such result = broad bloat at low precision → content-origin follow-up.
    "read_file", "grep_files", "glob_files", "list_directory", "sandboxed_exec",
    "shell",  # #2593: pipeline `shell` step sugar over sandboxed_exec — same exec-output class
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
    # #2103 S1bc: session_spawn → async dispatch, returns a "spawned" ACK
    # {status, sid, mode}, not the spawned session's output (result-routing-back is
    # the S1bc-exec/Stage-4 follow-on, fenced there).
    "session_spawn",
    # #2103 B-tool: agent_spawn → returns an OS-generated spawn ACK
    # {status, name, parent, note}, not external content (it creates an agent; any
    # output the new agent later produces is fenced on its own path).
    "agent_spawn",
    # #2103 C1: topology_create → returns an OS-generated create ACK
    # {status, name, kind, members, ...}, not external content (it wires a topology).
    "topology_create",
    # — writes / installs / deletes: return status, not external content —
    "write_file", "edit_file", "delete_file", "drop_source",
    "remember_shared", "remember_agent", "forget_memory",
    "mcp_install", "mcp_install_local", "mcp_install_package",
    "mcp_install_registry", "mcp_drop_server",
    # #2548 PR-C: skill_install_local writes .reyn/config/skills.yaml — returns an
    # install status dict (path / name / status), not fetched external content.
    # Same classification rationale as mcp_install_local (writes config, not content).
    "skill_install_local",
    # #2548 PR-D: skill_install_source shallow-clones a git repo and writes
    # .reyn/config/skills.yaml — returns an install status dict, not the fetched
    # repo content. The cloned SKILL.md is threat-scanned before registration;
    # the scan result is internal OS state, not forwarded external content.
    # Same classification rationale as mcp_install_package (installs, does not relay).
    # #2597 slice ②b: subscribe_mcp_resource / unsubscribe_mcp_resource return an
    # {status, server, uri} subscribe-confirmation ACK, never resource CONTENT (the
    # push notification itself carries no payload — a caller re-reads via
    # read_mcp_resource, which IS fenced above). Same "status ACK, not content"
    # classification rationale as mcp_install_local / topology_create.
    "subscribe_mcp_resource", "unsubscribe_mcp_resource",
    "skill_install_source",
    # pipeline_install_local writes .reyn/config/pipelines.yaml — returns an
    # install status dict (path / name / status), not fetched external content.
    # Same classification rationale as skill_install_local / mcp_install_local.
    "pipeline_install_local",
    # pipeline_install_source shallow-clones a git repo and writes
    # .reyn/config/pipelines.yaml — returns an install status dict, not the
    # fetched repo content. The cloned DSL description is threat-scanned before
    # registration; the scan result is internal OS state, not forwarded
    # external content. Same rationale as skill_install_source.
    "pipeline_install_source",
    "cron_register", "cron_unregister", "cron_enable", "cron_disable",
    # #2073 S3: hooks_add writes .reyn/hooks.yaml + schedules a reload — returns a
    # status dict (on / added / reload_scheduled / path), not external content.
    "hooks_add",
    # FP-0057 Phase 1: embed returns VECTORS (float arrays derived from the
    # input texts), not relayed external content — the numeric embedding is a
    # transform of the caller's own texts, not fetched server/internet content.
    # (The PRE-embed redaction-egress seam scrubs secrets before the outbound
    # API call; the returned vectors carry no external payload.) Same "derived
    # from input, not a relay" rationale as render_template.
    "embed",
    # — catalog / discovery (reyn-assembled or operator config) —
    "list_agents", "describe_agent",
    "list_actions", "search_actions", "describe_action",
    "list_mcp_servers", "cron_list",
    # — presentation (#2692, part of the #2688 sweep) —
    # present: fire-and-continue → returns a compact ACK (reached-user + view-bind
    # stats), NOT the presented data itself → no external content forwarded (same
    # "status ACK, not content" rationale as topology_create / the installers).
    "present",
    # render_template: returns the rendered string, derived from a template + data.
    # A data_ref/template_ref reads file content — the same agent-work-product /
    # file-content class as read_file (the deferred fast-follow, scan-only), not a
    # relay of server/internet content.
    "render_template",
    # — control / orchestration —
    "compact",
    # invoke_action: generic dispatcher — trust resolved by the EFFECTIVE inner
    # name at dispatch() (the dispatch-tag), not by this wrapper.
    "invoke_action",
    # — reyn's own framework source (trusted) —
    "reyn_src_list", "reyn_src_read", "reyn_src_glob", "reyn_src_grep",
    # — task subsystem (#1953 dynamic-wire) — return the OS task RECORD (id /
    # status / deps / the task's own fields) from the CAS-gated task backend:
    # structured OS state, not external content. Cross-session content (a
    # delegated task's description / a peer's comment) crosses the trust
    # boundary at the WAKE / result inbound seam (items 4-5), fenced THERE —
    # the same pattern as delegate_to_agent (ACK here, reply fenced inbound).
    "task.create", "task.update_status", "task.get", "task.list",
    "task.add_dependency", "task.remove_dependency", "task.repoint_dependency",
    "task.abort", "task.heartbeat", "task.register_unblock_predicate",
    "task.comment", "task.assign",
    # IS-1 (pipeline v0.9 R6): run_pipeline returns the pipeline's OWN final
    # output (run_id / output / named_stores) — an OS-assembled result of
    # internal step execution, not fetched external content. Any external
    # content a tool/agent step's own result carries is fenced on THAT step's
    # own tool-result path when it runs (same "ACK here, fenced at its own
    # seam" pattern as delegate_to_agent / session_spawn above).
    "run_pipeline",
    # IS-2: run_pipeline_async returns only {status: started, run_id} — an
    # OS-assembled launch ACK, no content at all. The eventual result arrives
    # as an OS-framed pipeline_result inbox message; any external content a
    # step fetched was fenced at that step's own tool-result seam when it
    # ran (same rationale as run_pipeline above).
    "run_pipeline_async",
    # IS-4: run_pipeline_inline returns the ad-hoc pipeline's OWN final output
    # (run_id / output / named_stores), and run_pipeline_inline_async returns
    # only {status: started, run_id} — identical OS-assembled framing to
    # run_pipeline / run_pipeline_async respectively. The definition is an
    # AGENT-GENERATED DSL string, not fetched external content; any external
    # content a step pulls is fenced at THAT step's own tool-result seam when it
    # runs (same "ACK here, fenced at its own seam" rationale as above).
    "run_pipeline_inline",
    "run_pipeline_inline_async",
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
