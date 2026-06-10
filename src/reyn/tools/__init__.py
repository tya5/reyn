"""Reyn unified tool registry — single source of truth for capabilities
exposed to both router-style (function calling) and phase-style
(Control IR JSON output) LLM invocations.

Per ADR-0026 (Status: Proposed). M1 lays the infrastructure;
capability migrations land in M2/M3.
"""
from reyn.tools.registry import ToolRegistry
from reyn.tools.types import (
    PhaseCallerState,
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
    "PhaseCallerState",
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
    from reyn.tools.ask_user import ASK_USER
    from reyn.tools.catalog import (
        DESCRIBE_AGENT,
        DESCRIBE_SKILL,
        LIST_AGENTS,
        LIST_SKILLS,
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
    from reyn.tools.invoke_skill import INVOKE_SKILL
    from reyn.tools.lint import LINT
    from reyn.tools.mcp import (
        CALL_MCP_TOOL,
        DESCRIBE_MCP_TOOL,
        LIST_MCP_SERVERS,
        LIST_MCP_TOOLS,
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
    from reyn.tools.plan import PLAN
    from reyn.tools.recall import RECALL
    from reyn.tools.reyn_src import (
        REYN_SRC_GLOB,
        REYN_SRC_GREP,
        REYN_SRC_LIST,
        REYN_SRC_READ,
    )
    from reyn.tools.sandboxed_exec import SANDBOXED_EXEC

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
    registry.register(INVOKE_SKILL)
    # RAG ops (ADR-0033 Phase 1)
    registry.register(RECALL)
    registry.register(DROP_SOURCE)
    registry.register(COMPACT)
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
    # Memory ops (Wave 2 — Type C closure: memory write phase-side)
    registry.register(LIST_MEMORY)
    registry.register(READ_MEMORY_BODY)
    registry.register(REMEMBER_SHARED)
    registry.register(REMEMBER_AGENT)
    registry.register(FORGET_MEMORY)
    # Catalog ops (Wave 2 — Type C closure: catalog browse phase-side)
    registry.register(LIST_SKILLS)
    registry.register(DESCRIBE_SKILL)
    registry.register(LIST_AGENTS)
    registry.register(DESCRIBE_AGENT)
    # ── Exec / lint / ask_user (gates declared per-tool) ──
    # #1352-D: SANDBOXED_EXEC is router="allow" (chat-reachable; the exec
    # category is additionally gated by is_exec_available = a real sandbox
    # backend, not by gates.router) — it was previously mis-grouped under a
    # "gates.router=deny" comment alongside the now-removed `shell` op (the only
    # true router=deny here was shell / ask_user). LINT=router="allow",
    # ASK_USER=router="deny".
    registry.register(SANDBOXED_EXEC)
    registry.register(LINT)
    registry.register(ASK_USER)
    # ── Router-only capabilities (gates.router=allow, gates.phase=deny) ──
    registry.register(DELEGATE_TO_AGENT)
    registry.register(PLAN)
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
    # FP-0038 (#171) S2 + S3: glob / grep for Reyn's own repo, mirroring
    # the file__glob / file__grep surfaces but scoped to the OS source tree.
    registry.register(REYN_SRC_GLOB)
    registry.register(REYN_SRC_GREP)
    # ── Phase-only coarse-name ops (gates.router=deny, gates.phase=allow) ─
    # #1240 Wave 2b: MCP_OP + RUN_SKILL_OP coarse ToolDefinitions dropped.
    # Phase advertises "call_mcp_tool" / "invoke_skill" via available_ops();
    # the (A)-alias in _PHASE_TOOL_NAME_ALIAS rewrites to "mcp"/"run_skill"
    # at the parse boundary.  Dispatch falls to the legacy execute_op path
    # (op_runtime/mcp.py + op_runtime/run_skill.py via register("mcp"/"run_skill")).
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
