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
    from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
    from reyn.tools.drop_source import DROP_SOURCE
    from reyn.tools.embed import EMBED

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
    from reyn.tools.mcp import (
        CALL_MCP_TOOL,
        DESCRIBE_MCP_TOOL,
        GET_MCP_PROMPT,
        LIST_MCP_PROMPTS,
        LIST_MCP_RESOURCE_TEMPLATES,
        LIST_MCP_RESOURCES,
        LIST_MCP_SERVERS,
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
        RUN_PIPELINE,
        RUN_PIPELINE_ASYNC,
        RUN_PIPELINE_INLINE,
        RUN_PIPELINE_INLINE_ASYNC,
    )
    from reyn.tools.present import PRESENT
    from reyn.tools.recall import RECALL
    from reyn.tools.render_template import RENDER_TEMPLATE
    from reyn.tools.reyn_src import (
        REYN_SRC_GLOB,
        REYN_SRC_GREP,
        REYN_SRC_LIST,
        REYN_SRC_READ,
    )
    from reyn.tools.sandboxed_exec import SANDBOXED_EXEC
    from reyn.tools.session_spawn import SESSION_SPAWN
    from reyn.tools.shell import SHELL
    from reyn.tools.skill_verbs import SKILL_INSTALL_LOCAL, SKILL_INSTALL_SOURCE
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
    # ── Both-surface capabilities (gates.router=allow, gates.phase=allow) ──
    registry.register(WEB_SEARCH)
    registry.register(WEB_FETCH)
    # #1449: read_tool_result retired — its same-host path-ref read is covered by
    # file__read(path) (the refs are plain files under .reyn/tool-results/), and
    # its image guard is superseded by file__read's #365 media-blocks + #1449
    # binary guard. The cross-host resource_uri path was a never-implemented stub.
    # RAG ops (ADR-0033 Phase 1)
    registry.register(RECALL)
    registry.register(DROP_SOURCE)
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
    # Task ops (#1953 dynamic-wire): the 12 task.* ToolDefinitions, derived
    # single-source from the IROp models (tools/task_ops.py).
    from reyn.tools.task_ops import TASK_TOOL_DEFINITIONS
    for _task_def in TASK_TOOL_DEFINITIONS:
        registry.register(_task_def)
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
    # MCP ops (Wave 2 — Type C closure: phase-side discover)
    # FP-0032: DESCRIBE_MCP_TOOL added as D4 (mirror of describe_skill).
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
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
    # Memory ops (Wave 2 — Type C closure: memory write phase-side)
    registry.register(LIST_MEMORY)
    registry.register(READ_MEMORY_BODY)
    registry.register(REMEMBER_SHARED)
    registry.register(REMEMBER_AGENT)
    registry.register(FORGET_MEMORY)
    # Catalog ops (Wave 2 — Type C closure: catalog browse phase-side)
    registry.register(LIST_AGENTS)
    registry.register(DESCRIBE_AGENT)
    # ── Exec / lint / ask_user (gates declared per-tool) ──
    # #1352-D: SANDBOXED_EXEC is router="allow" (chat-reachable; the exec
    # category is additionally gated by is_exec_available = a real sandbox
    # backend, not by gates.router) — it was previously mis-grouped under a
    # "gates.router=deny" comment alongside the now-removed `shell` op (the only
    # true router=deny here was shell / ask_user). ASK_USER=router="deny".
    registry.register(SANDBOXED_EXEC)
    # #2593: pipeline DSL `shell` step sugar — a bare-registry tool (no
    # invoke_action qualified route; a pipeline `tool` step resolves it by
    # bare name via ``pipeline_verbs._make_tool_dispatch``'s bare-lookup
    # fallback, same as SANDBOXED_EXEC's own qualified route falls back for
    # unqualified callers).
    registry.register(SHELL)
    registry.register(ASK_USER)
    # ── Router-only capabilities (gates.router=allow, gates.phase=deny) ──
    registry.register(DELEGATE_TO_AGENT)
    registry.register(SESSION_SPAWN)
    registry.register(AGENT_SPAWN)
    registry.register(TOPOLOGY_CREATE)
    registry.register(REYN_SRC_LIST)
    registry.register(REYN_SRC_READ)
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
    # FP-0038 (#171) S2 + S3: glob / grep for Reyn's own repo, mirroring
    # the file__glob / file__grep surfaces but scoped to the OS source tree.
    registry.register(REYN_SRC_GLOB)
    registry.register(REYN_SRC_GREP)
    # ── Phase-only coarse-name ops (gates.router=deny, gates.phase=allow) ─
    # #1240 Wave 2b: MCP_OP coarse ToolDefinition dropped.
    # Phase advertises "call_mcp_tool" via available_ops(); the (A)-alias in
    # _PHASE_TOOL_NAME_ALIAS rewrites it to "mcp" at the parse boundary.
    # Dispatch falls to the legacy execute_op path (op_runtime/mcp.py via
    # register("mcp")).
    # NOTE: FILE_OP coarse ToolDefinition was dropped in the previous Wave 2b step.
    registry.register(MCP_INSTALL_OP)
    # FP-0034 §D23: mcp_drop_server is router+phase callable (= dual gate).
    # Reachable via universal_action ``mcp.operation__drop_server`` AND
    # as a phase Control IR op kind="mcp_drop_server".
    registry.register(MCP_DROP_SERVER_OP)
    # Issue #879: verb-object MCP wrappers — pure op-runtime handlers
    # (no skill spawn) under the new ``mcp`` category in _OPERATION_RULES.
    registry.register(MCP_SEARCH_REGISTRY)
    registry.register(MCP_INSTALL_REGISTRY)
    registry.register(MCP_INSTALL_PACKAGE)
    registry.register(MCP_INSTALL_LOCAL)
    registry.register(MCP_CALL_TOOL)
    # #2548 PR-C: skill install verb (local SKILL.md dir registration).
    registry.register(SKILL_INSTALL_LOCAL)
    # #2548 PR-D: skill install verb (git/GitHub URL source fetch).
    registry.register(SKILL_INSTALL_SOURCE)
    # pipeline install verbs (local DSL file registration + git/GitHub URL
    # source fetch) — mirrors SKILL_INSTALL_LOCAL / SKILL_INSTALL_SOURCE.
    registry.register(PIPELINE_INSTALL_LOCAL)
    registry.register(PIPELINE_INSTALL_SOURCE)
    # IS-1 (pipeline v0.9 R6): run_pipeline — sync launch of a REGISTERED
    # pipeline. Router+phase allow. IS-5: surfaced to the live LLM catalog
    # via the ``pipeline`` universal-catalog category enumerator (lists
    # registered pipelines) + invoke_action (``pipeline__run`` /
    # ``run_pipeline``) — the same PR-3b-shipped path every other
    # universal-catalog wrapper uses, NOT build_tools() (which is
    # hand-assembled and strips direct tools once wrappers are on).
    registry.register(RUN_PIPELINE)
    # IS-2: run_pipeline_async — background launch in a crash-recoverable
    # driver-session; returns {status: started, run_id} immediately, the
    # result arrives later as a pipeline_result inbox message.
    registry.register(RUN_PIPELINE_ASYNC)
    # IS-4: run_pipeline_inline / run_pipeline_inline_async — launch an ad-hoc,
    # agent-GENERATED pipeline (a DSL string in 'definition'), parsed + put
    # through a static-analysis gate before spawn, then run through the SAME
    # attached / background driver-session the registered verbs use. Recovery is
    # identical (invocation.json carries the full serialized Pipeline).
    registry.register(RUN_PIPELINE_INLINE)
    registry.register(RUN_PIPELINE_INLINE_ASYNC)
    # ── FP-0034 universal catalog wrappers (router-only) ─────────────────
    # PR-3a registers them in the registry; PR-3b will add them to
    # build_tools() output and refactor the SP. Handlers wire through
    # universal_dispatch.py routing back into THIS registry to invoke
    # the canonical target ToolDefinition.
    registry.register(LIST_ACTIONS)
    registry.register(SEARCH_ACTIONS)
    registry.register(DESCRIBE_ACTION)
    registry.register(INVOKE_ACTION)
    return registry
