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
    # Proposal 0067 P4d (#3978): run_prompt(collect="attached") returns the
    # PEER SESSION's own reply text SYNCHRONOUSLY as THIS tool's result —
    # unlike delegate_to_agent (returns only a spawn ACK; the real reply is
    # fenced later at the A3 inbound seam when it arrives) and send_to_session
    # (fire-and-forget; there is no reply to relay at all). This IS the seam
    # where that content needs fencing: the peer's own turn may have read
    # web/file/exec content and folded it into its reply, same "agent-written"
    # rationale as list_memory/read_memory_body above.
    "run_prompt",
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
    # #2971: skill descriptions are operator- or third-party-authored text —
    # skill_install_source registers them straight out of a fetched git repo.
    # They are threat-scanned at install, but skill_list re-surfaces them on
    # every later call, when a scan-rule update may have changed the verdict.
    # Same rationale as list_mcp_tools' server-authored descriptions.
    "skill_list",
    # #3026: pipeline descriptions are operator- or third-party-authored —
    # pipeline_install_source registers them straight out of a fetched
    # git repo, exactly as skill_install_source does. pipeline_list re-surfaces
    # them on every later call. Same rationale as skill_list directly above.
    "pipeline_list",
    # FP-0066 P1b: "semantic_search" and "list_rag_sources" are removed from
    # this set — the agent-facing layer-1 in-core RAG tools are retired. See
    # docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.
    # FP-0066 P3c (#3247 firm §2): search_knowledge's role is DISCOVERY — it
    # re-surfaces operator/user-authored skill/memory/repo text (search
    # results, not activation) without activating it. Same role class as
    # skill_list above — the symmetric OPPOSITE of load_skill (_NOT_EXTERNAL,
    # activation, below): discovery=fenced / activation=not-fenced, the same
    # distinction #3255/#3254 established for skill_list vs load_skill.
    "search_knowledge",
}

# Not fenced (returns_external_content=False): each justified below. Scan-all
# still runs on these at the chokepoint (detection completeness).
_NOT_EXTERNAL = {
    # — deferred to the tracked fast-follow (scan-only in S2; FP-0050 §6) —
    # file content / exec output: agent work-products, secondary vector; fencing
    # every such result = broad bloat at low precision → content-origin follow-up.
    "read_file", "grep_files", "glob_files", "list_directory", "exec",
    # — principal / peer (lead finding: explicitly classified) —
    # ask_user: the user is the trust ROOT — their input is the legitimate
    # instruction channel, not untrusted-data (fencing it would break the
    # user-message-as-instruction model). User-relayed paste is out of the S2
    # threat model (principal's own channel).
    "ask_user",
    # #2103 S1bc: spawn_session → async dispatch, returns a "spawned" ACK
    # {status, sid, mode}, not the spawned session's output (result-routing-back is
    # the S1bc-exec/Stage-4 follow-on, fenced there).
    "spawn_session",
    # #2103 B-tool: spawn_agent → returns an OS-generated spawn ACK
    # {status, name, parent, note}, not external content (it creates an agent; any
    # output the new agent later produces is fenced on its own path).
    "spawn_agent",
    # #2103 C1: create_topology → returns an OS-generated create ACK
    # {status, name, kind, members, ...}, not external content (it wires a topology).
    "create_topology",
    # Proposal 0067 P5 (#3978): send_to_session → returns an OS-generated
    # delivery ACK {status, agent, session, wake}, not external content — it
    # is fire-and-forget delivery, so there is no reply to relay at all.
    "send_to_session",
    # — writes / installs / deletes: return status, not external content —
    "write_file", "edit_file", "delete_file",
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
    # classification rationale as mcp_install_local / create_topology.
    "subscribe_mcp_resource", "unsubscribe_mcp_resource",
    "skill_install_source",
    # FP-0066 P0 (#3254): load_skill returns a skill body that becomes agent
    # INSTRUCTIONS (load = fetch+activate), not untrusted data to fence.
    # Refactor-neutrality (architect ruling): the pre-P0 path read skill
    # bodies via read_file, which is _NOT_EXTERNAL (above) — P0 is a
    # responsibility-extraction refactor and must not smuggle a trust-semantics
    # flip (not-fenced -> fenced). Fencing would mark the body "as data"
    # (inter-agent taint), degrading activation: the body is meant to work AS
    # instructions. Differs from skill_list (_EXTERNAL) by ROLE — discovery/
    # listing of re-surfaced metadata vs activation. (#1909: external_source
    # taint-narrowing does not fire for a _NOT_EXTERNAL load; a load_skill-
    # specific opt-in taint is a separate arc, not this binary gate.)
    "load_skill",
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
    # proposal 0060 Phase 1 Layer A (A8): presentation_install_local writes
    # .reyn/config/presentations.yaml — returns an install status dict
    # (name / config_path / status), not fetched external content. No git/source
    # fetch path at all (a blueprint is inline declarative data). Same
    # classification rationale as skill_install_local / pipeline_install_local.
    "presentation_install_local",
    # ADR 0064 plugin model P2: install_plugin copies a plugin dir
    # + registers its capabilities — returns an install status dict (name /
    # plugin_root / capabilities / registered), not fetched external content.
    # A {kind:"git"} source's cloned files are threat-scanned (via the SAME
    # skill_install/pipeline_install sub-handlers this delegates to) before
    # registration; the scan result is internal OS state, not forwarded
    # external content. Same classification rationale as skill_install_source
    # / mcp_install_package (installs, does not relay).
    # uninstall_plugin returns a removal status dict (name /
    # removed / copy_removed) — same "status ACK, not content" rationale as
    # mcp_drop_server.
    "install_plugin", "uninstall_plugin",
    # #3202 symptom 3: list_plugins ONLY enumerates BUILTIN_PLUGINS
    # -- reyn's own shipped plugin directories and their own reyn-authored
    # plugin.json manifests. Unlike skill_list/pipeline_list
    # (which can surface operator/third-party text registered via a
    # {kind:"local"/"git"} install), there is no local/git listing here, so
    # no third-party text ever flows through this handler.
    "list_plugins",
    "cron_register", "cron_unregister", "cron_enable", "cron_disable",
    # #2073 S3: hooks_add writes .reyn/hooks.yaml + schedules a reload — returns a
    # status dict (on / added / reload_scheduled / path), not external content.
    "hooks_add",
    # Hook-Event Redesign Phase 5 part 2: emit_hook_event publishes an
    # LLM-authored hook-event to this session's OWN internal per-Session
    # HookBus and returns a status dict (kind / status / emitted_kind), never
    # external/untrusted content — the payload is LLM-authored (already inside
    # the trust boundary), not fetched from a server/internet/foreign session.
    "emit_hook_event",
    # FP-0057 Phase 1: embed returns VECTORS (float arrays derived from the
    # input texts), not relayed external content — the numeric embedding is a
    # transform of the caller's own texts, not fetched server/internet content.
    # (The PRE-embed redaction-egress seam scrubs secrets before the outbound
    # API call; the returned vectors carry no external payload.) Same "derived
    # from input, not a relay" rationale as render_template.
    "embed",
    # FP-0066 P1b: "index_update" is removed from this set — the agent-facing
    # layer-1 in-core RAG tool is retired (the OS-internal op is kept).
    # — catalog / discovery (reyn-assembled or operator config) —
    "list_agents", "describe_agent",
    "list_actions", "search_actions", "describe_action",
    "list_mcp_servers", "cron_list",
    # #4686: list_mcp_subscriptions — NOT the same class as list_mcp_tools/
    # list_mcp_resources/list_mcp_prompts above (server-AUTHORED free text:
    # descriptions, schemas, content — genuinely fenceable injection
    # surface). Every string this tool returns is bounded by URIs REYN
    # ITSELF already tracks (subscribe_mcp_resource's own argument, chosen
    # by the agent) — ``uris`` is that tracked set verbatim; ``unhonored``
    # is ``tracked - honored`` (connection_service.py's own
    # ``unhonored_uris``), a SET-DIFFERENCE against the tracked set, so it
    # can only ever be a SUBSET of already-known strings, never a new
    # server-authored string smuggled in. ``mode`` is reyn-computed (the
    # adapter class name), not server content either. Same "derived from
    # reyn's own state, not a relay of external content" rationale as
    # embed/render_template above — a malicious server can narrow which of
    # the agent's own URIs get marked unconfirmed, but cannot inject
    # arbitrary text through this response shape.
    "list_mcp_subscriptions",
    # — presentation (#2692, part of the #2688 sweep) —
    # present: fire-and-continue → returns a compact ACK (reached-user + view-bind
    # stats), NOT the presented data itself → no external content forwarded (same
    # "status ACK, not content" rationale as create_topology / the installers).
    "present",
    # render_template: returns the rendered string, derived from a template + data.
    # A data_ref/template_ref reads file content — the same agent-work-product /
    # file-content class as read_file (the deferred fast-follow, scan-only), not a
    # relay of server/internet content.
    "render_template",
    # — control / orchestration —
    "compact",
    # describe_session: OS-internal introspection (write scope declared by
    # the operator's own reyn.yaml, git/venv/toolchain facts, auth status
    # against reyn's own token store) — never external network/store/disk
    # content (#5012-A).
    "describe_session",
    # invoke_action: generic dispatcher — trust resolved by the EFFECTIVE inner
    # name at dispatch() (the dispatch-tag), not by this wrapper.
    "invoke_action",
    # — reyn's own framework source (trusted) —
    "reyn_repo_list", "reyn_repo_read", "reyn_repo_glob", "reyn_repo_grep",
    # IS-1/IS-2/IS-4 (pipeline v0.9 R6), unified into one name by proposal 0067
    # P7 (#3978, 4 names -> 1, 0 aliases): ``run_pipeline`` (registered via
    # ``name=`` or ad-hoc via ``definition=``) returns EITHER the pipeline's
    # OWN final output (run_id / output / named_stores, ``collect="attached"``)
    # OR only {status: started, run_id} (an OS-assembled launch ACK,
    # ``collect="async"``) — both OS-assembled framings, never fetched
    # external content itself. For ``definition=``, the DSL string is
    # AGENT-GENERATED, not fetched external content either. Any external
    # content a tool/agent step's own result carries is fenced on THAT step's
    # own tool-result path when it runs (same "ACK here, fenced at its own
    # seam" pattern as delegate_to_agent / spawn_session above); for the async
    # collect, the eventual result arrives as an OS-framed pipeline_result
    # inbox message, fenced the same way at the step that produced it.
    "run_pipeline",
    # proposal 0067 P4 (#3978): describe_task / list_tasks / cancel_task all
    # return OS-assembled structured data (task_id/kind/status/session/
    # requester, drawn from ChainManager's own in-memory state) — no
    # external content of any kind.
    "describe_task",
    "list_tasks",
    "cancel_task",
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
