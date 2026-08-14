"""Reyn unified tool registry — single source of truth for capabilities
exposed to router-style (function calling) LLM invocations.

Per ADR-0026 (Status: Proposed). M1 lays the infrastructure;
capability migrations land in M2/M3.
"""
from reyn.tools.registry import ToolRegistry
from reyn.tools.types import (
    RouterCallerState,
    ToolContext,
    ToolDefinition,
    ToolGates,
    ToolHandler,
    ToolResult,
)

__all__ = [
    "ToolDefinition",
    "ToolGates",
    "ToolContext",
    "RouterCallerState",
    "ToolHandler",
    "ToolResult",
    "ToolRegistry",
    "get_default_registry",
]


def get_default_registry() -> ToolRegistry:
    """Build and return the default ToolRegistry with all migrated capabilities.

    M2: web_search is the first capability in the registry.
    M3: additional capabilities will be registered here as they migrate.

    Returns a fresh ToolRegistry instance each call (lightweight construction;
    callers may cache the result if needed).
    """
    # Lazy import to avoid circular dependencies at package-init time.
    from reyn.tools.agent_spawn import AGENT_SPAWN
    from reyn.tools.ask_user import ASK_USER
    from reyn.tools.catalog import (
        DESCRIBE_AGENT,
        LIST_AGENTS,
    )
    from reyn.tools.compact import COMPACT
    from reyn.tools.cron import (
        CRON_DISABLE,
        CRON_ENABLE,
        CRON_LIST,
        CRON_REGISTER,
        CRON_UNREGISTER,
    )
    from reyn.tools.embed import EMBED
    from reyn.tools.emit_hook_event import EMIT_HOOK_EVENT
    from reyn.tools.exec import EXEC

    # Wave 2 additions (ADR-0026 M3 Wave 2)
    from reyn.tools.file import (
        DELETE_FILE,
        EDIT_FILE,
        GLOB_FILES,
        GREP_FILES,
        LIST_DIRECTORY,
        READ_FILE,
        WRITE_FILE,
    )
    from reyn.tools.hooks import HOOKS_ADD
    from reyn.tools.knowledge import SEARCH_KNOWLEDGE
    from reyn.tools.mcp import (
        CALL_MCP_TOOL,
        DESCRIBE_MCP_TOOL,
        GET_MCP_PROMPT,
        LIST_MCP_PROMPTS,
        LIST_MCP_RESOURCE_TEMPLATES,
        LIST_MCP_RESOURCES,
        LIST_MCP_SERVERS,
        LIST_MCP_SUBSCRIPTIONS,
        LIST_MCP_TOOLS,
        READ_MCP_RESOURCE,
        SUBSCRIBE_MCP_RESOURCE,
        UNSUBSCRIBE_MCP_RESOURCE,
    )
    from reyn.tools.mcp_drop import MCP_DROP_SERVER_OP
    from reyn.tools.mcp_install import MCP_INSTALL_OP
    from reyn.tools.mcp_verbs import (
        MCP_CALL_TOOL,
        MCP_INSTALL_LOCAL,
        MCP_INSTALL_PACKAGE,
        MCP_INSTALL_REGISTRY,
        MCP_SEARCH_REGISTRY,
    )
    from reyn.tools.memory import (
        FORGET_MEMORY,
        LIST_MEMORY,
        READ_MEMORY_BODY,
        REMEMBER_AGENT,
        REMEMBER_SHARED,
    )
    from reyn.tools.pipeline_management_verbs import (
        PIPELINE_INSTALL_LOCAL,
        PIPELINE_INSTALL_SOURCE,
    )
    from reyn.tools.pipeline_verbs import (
        PIPELINE_LIST,
        RUN_PIPELINE,
    )
    from reyn.tools.plugin_management_verbs import PLUGIN_INSTALL, PLUGIN_LIST, PLUGIN_UNINSTALL
    from reyn.tools.present import PRESENT
    from reyn.tools.presentation_management_verbs import PRESENTATION_INSTALL
    from reyn.tools.render_template import RENDER_TEMPLATE
    from reyn.tools.reyn_repo import (
        REYN_REPO_GLOB,
        REYN_REPO_GREP,
        REYN_REPO_LIST,
        REYN_REPO_READ,
    )
    from reyn.tools.run_prompt import RUN_PROMPT
    from reyn.tools.send_to_session import SEND_TO_SESSION
    from reyn.tools.session_spawn import SESSION_SPAWN
    from reyn.tools.skill_verbs import (
        LOAD_SKILL,
        SKILL_INSTALL_LOCAL,
        SKILL_INSTALL_SOURCE,
        SKILL_LIST,
    )
    from reyn.tools.task_verbs import CANCEL_TASK, DESCRIBE_TASK, LIST_TASKS
    from reyn.tools.topology_create import TOPOLOGY_CREATE

    # FP-0034 PR-3a: universal catalog wrappers (registered in registry;
    # not yet added to router build_tools() — that lands in PR-3b).
    from reyn.tools.universal_catalog import (
        DESCRIBE_ACTION,
        INVOKE_ACTION,
        LIST_ACTIONS,
        SEARCH_ACTIONS,
    )
    from reyn.tools.web_fetch import WEB_FETCH
    from reyn.tools.web_search import WEB_SEARCH

    registry = ToolRegistry()
    # ── Router-surfaced capabilities (gates.router=allow) ──
    registry.register(WEB_SEARCH)
    registry.register(WEB_FETCH)
    # #1449: read_tool_result retired — its same-host path-ref read is covered by
    # read_file(path) (the refs are plain files under .reyn/tool-results/), and
    # its image guard is superseded by read_file's #365 media-blocks + #1449
    # binary guard. The cross-host resource_uri path was a never-implemented stub.
    # FP-0066 P1b: semantic_search / drop_source / index_update / list_rag_sources
    # (the agent-facing layer-1 in-core RAG tools, ADR-0033 Phase 1 / FP-0057
    # Phase 2a / #3026) are RETIRED — they were a pre-audience-split relic (user-RAG
    # semantics riding the OS-internal store). The OS-internal substrate they rode
    # (IndexUpdateIROp / SemanticSearchIROp / SqliteIndexBackend) is kept — see
    # docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md §9.
    # FP-0057 Phase 1: raw embed primitive (user-facing; composes with an
    # external MCP vector-DB via pipeline — reyn hosts no user RAG store).
    registry.register(EMBED)
    registry.register(COMPACT)
    # #2692 (part of the #2688 sweep): present + render_template invocation surface.
    # One registration each opens BOTH chat (build_tools + gates.router="allow") and
    # pipeline (bare-name lookup) from the single unified registry — the op handlers
    # already existed; only the ToolDefinition was missing.
    registry.register(PRESENT)
    registry.register(RENDER_TEMPLATE)
    # File ops (Wave 2 — Open Q #6 fine-grained naming)
    registry.register(READ_FILE)
    registry.register(WRITE_FILE)
    registry.register(DELETE_FILE)
    registry.register(LIST_DIRECTORY)
    registry.register(GREP_FILES)
    registry.register(GLOB_FILES)
    # FP-0040 (#178): partial-edit op so the LLM can patch by unique-string
    # anchor instead of full-file read+write round-trip.
    registry.register(EDIT_FILE)
    # MCP ops (Wave 2)
    # FP-0032: DESCRIBE_MCP_TOOL added as D4 (mirror of describe_skill).
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
    # #4686: per-connection subscription state — never touches the network
    # (session-local), placed beside LIST_MCP_SERVERS as the other
    # no-args/no-gateway MCP discovery verb.
    registry.register(LIST_MCP_SUBSCRIPTIONS)
    registry.register(LIST_MCP_TOOLS)
    registry.register(DESCRIBE_MCP_TOOL)
    # #2597 slice ②a: resources consumption (list/read/templates) — parallel
    # to the tools surface above.
    registry.register(LIST_MCP_RESOURCES)
    registry.register(LIST_MCP_RESOURCE_TEMPLATES)
    registry.register(READ_MCP_RESOURCE)
    # #2597 slice ②b: resource subscriptions.
    registry.register(SUBSCRIBE_MCP_RESOURCE)
    registry.register(UNSUBSCRIBE_MCP_RESOURCE)
    # #2597 slice ②c: prompts consumption (list/get).
    registry.register(LIST_MCP_PROMPTS)
    registry.register(GET_MCP_PROMPT)
    # Memory ops (Wave 2)
    registry.register(LIST_MEMORY)
    registry.register(READ_MEMORY_BODY)
    registry.register(REMEMBER_SHARED)
    registry.register(REMEMBER_AGENT)
    registry.register(FORGET_MEMORY)
    # Catalog ops (Wave 2)
    registry.register(LIST_AGENTS)
    registry.register(DESCRIBE_AGENT)
    # ── Exec / lint / ask_user (gates declared per-tool) ──
    # #1352-D: EXEC is router="allow" (chat-reachable; the exec
    # category is additionally gated by is_exec_available = a real sandbox
    # backend, not by gates.router) — it was previously mis-grouped under a
    # "gates.router=deny" comment alongside the now-removed `shell` op (the only
    # true router=deny here was ask_user). #3226 Phase 1: the #2593 pipeline
    # DSL `shell` tool (thin sugar building `/bin/sh -c <command>` over this
    # same EXEC, then named `sandboxed_exec`) is removed outright — the sole
    # `/bin/sh -c <str>` injection surface in the codebase. #3226 Phase 3
    # renamed the tool `sandboxed_exec` -> `exec`. ASK_USER=router="deny".
    registry.register(EXEC)
    registry.register(ASK_USER)
    # ── Router-only capabilities (gates.router=allow) ──
    # delegate_to_agent retired (proposal 0067 P6, #3978) — send_to_session /
    # run_prompt reach another agent's context the same way; the relay/
    # completion substrate (PR14 pending_chain) stays live as run_prompt
    # (collect="async")'s own producer.
    registry.register(SESSION_SPAWN)
    registry.register(AGENT_SPAWN)
    registry.register(TOPOLOGY_CREATE)
    registry.register(SEND_TO_SESSION)  # proposal 0067 P5 (#3978)
    registry.register(RUN_PROMPT)  # proposal 0067 P4d (#3978), collect="attached" only
    registry.register(REYN_REPO_LIST)
    registry.register(REYN_REPO_READ)
    # FP-0041 #489 PR-B2: cron action category (= LLM-callable cron
    # job management). CRON_LIST is both-surface (read_only); the
    # 4 mutating ops are router-only.
    registry.register(CRON_REGISTER)
    registry.register(CRON_UNREGISTER)
    registry.register(CRON_LIST)
    registry.register(CRON_ENABLE)
    registry.register(CRON_DISABLE)
    # #2073 S3: the hooks-write self-reload tool (the agent adds its own runtime
    # hooks to .reyn/hooks.yaml + reloads at the turn boundary). Router-only.
    registry.register(HOOKS_ADD)
    # Hook-Event Redesign Phase 5 part 2 (proposal 0059 §8): LLM-authored
    # hook-event emission onto the caller's own HookBus. Router-only (the
    # handler needs a live session-bound HookBus/session_id).
    registry.register(EMIT_HOOK_EVENT)
    # FP-0038 (#171) S2 + S3: glob / grep for Reyn's own repo, mirroring
    # the glob_files / grep_files surfaces but scoped to the OS source tree.
    registry.register(REYN_REPO_GLOB)
    registry.register(REYN_REPO_GREP)
    # ── Coarse-name ops ──────────────────────────────────────────────────
    # #1240 Wave 2b dropped the coarse MCP_OP / FILE_OP ToolDefinitions.
    # MCP_INSTALL_OP keeps gates.router="deny": install must run through
    # op_runtime, so it is never advertised in the router's tools=. The phase
    # surface that used to invoke it is gone (#2434 / #2438); the bare-name
    # pipeline-step path still reaches it (#2696).
    registry.register(MCP_INSTALL_OP)
    # FP-0034 §D23: mcp_drop_server is reachable through the ``invoke_action``
    # wrapper as well as directly.
    registry.register(MCP_DROP_SERVER_OP)
    # Issue #879: verb-object MCP wrappers — pure op-runtime handlers
    # (no skill spawn) under the ``mcp`` category (universal_dispatch
    # ``_CATEGORY_ACTIONS``).
    registry.register(MCP_SEARCH_REGISTRY)
    registry.register(MCP_INSTALL_REGISTRY)
    registry.register(MCP_INSTALL_PACKAGE)
    registry.register(MCP_INSTALL_LOCAL)
    registry.register(MCP_CALL_TOOL)
    # #2548 PR-C: skill install verb (local SKILL.md dir registration).
    registry.register(SKILL_INSTALL_LOCAL)
    # #2548 PR-D: skill install verb (git/GitHub URL source fetch).
    registry.register(SKILL_INSTALL_SOURCE)
    # #2971: skill discovery verb — the surface that makes a non-menu skill
    # reachable at all (read-only; returns name/description/path for every
    # registered skill whose visibility is not "hidden").
    registry.register(SKILL_LIST)
    # FP-0066 P0 (#3247): dedicated skill-activation verb — replaces the
    # former file-read SKILL.md special-case.
    registry.register(LOAD_SKILL)
    # pipeline install verbs (local DSL file registration + git/GitHub URL
    # source fetch) — mirrors SKILL_INSTALL_LOCAL / SKILL_INSTALL_SOURCE.
    registry.register(PIPELINE_INSTALL_LOCAL)
    registry.register(PIPELINE_INSTALL_SOURCE)
    # proposal 0060 Phase 1 Layer A (A8): present-view install verb (register a
    # named presentation template) — mirrors SKILL_INSTALL_LOCAL / PIPELINE_INSTALL_LOCAL.
    registry.register(PRESENTATION_INSTALL)
    # ADR 0064 plugin model P2: plugin_install / plugin_uninstall — promote/
    # install and uninstall a self-contained plugin directory (security-critical:
    # composite of require_file_write (~/.reyn/plugins/, recursive) +
    # require_http_get (git fetch only — register-only since #3209, never a
    # dep-fetch)).
    registry.register(PLUGIN_INSTALL)
    registry.register(PLUGIN_UNINSTALL)
    # #3202 symptom 3: plugin discovery verb -- read-only enumeration of
    # BUILTIN_PLUGINS x each manifest, reachable from the ordinary tool-call
    # flow (not an install-error message).
    registry.register(PLUGIN_LIST)
    # IS-1 (pipeline v0.9 R6): run_pipeline — sync launch of a REGISTERED
    # pipeline. IS-5: surfaced to the live LLM catalog
    # via the ``pipeline`` universal-catalog category enumerator (lists
    # registered pipelines) + invoke_action (``run_pipeline`` /
    # ``run_pipeline``) — the same PR-3b-shipped path every other
    # universal-catalog wrapper uses, NOT build_tools() (which is
    # hand-assembled and strips direct tools once wrappers are on).
    # Proposal 0067 P7 (#3978): run_pipeline is now the unified launch verb —
    # collect="attached"|"async" replaces the sync/async name split, and
    # name=/definition= (exactly one) replaces the registered/inline split.
    # run_pipeline_async / run_pipeline_inline / run_pipeline_inline_async
    # are retired (4 names -> 1, 0 aliases, architect ruling).
    registry.register(RUN_PIPELINE)
    # #3026: pipeline discovery verb — the surface that NAMES the registered
    # pipelines. Constant-count replacement for the per-pipeline
    # ``pipeline__<name>`` catalog actions (which scaled the LLM payload with
    # the operator's pipeline count); ``run_pipeline``'s ``name`` argument is
    # unguessable without it.
    registry.register(PIPELINE_LIST)
    # proposal 0067 P4 (#3978): describe_task / list_tasks / cancel_task —
    # read/act against the settle-path handle substrate (ChainManager),
    # threaded via RouterCallerState.chains.
    registry.register(DESCRIBE_TASK)
    registry.register(LIST_TASKS)
    registry.register(CANCEL_TASK)
    # ── FP-0034 universal catalog wrappers (router-only) ─────────────────
    # PR-3a registers them in the registry; PR-3b will add them to
    # build_tools() output and refactor the SP. Handlers wire through
    # universal_dispatch.py routing back into THIS registry to invoke
    # the canonical target ToolDefinition.
    registry.register(LIST_ACTIONS)
    registry.register(SEARCH_ACTIONS)
    registry.register(DESCRIBE_ACTION)
    registry.register(INVOKE_ACTION)
    # FP-0066 P3c (#3247 firm §2/§3): search_knowledge — semantic search across
    # the operator's own skill/memory/repo knowledge (the ``knowledge`` category
    # in universal_dispatch._CATEGORY_ACTIONS).
    # Distinct from search_actions (tool-catalog search, above) — separate index,
    # separate role (discovery over knowledge content, not capability discovery).
    registry.register(SEARCH_KNOWLEDGE)
    return registry
