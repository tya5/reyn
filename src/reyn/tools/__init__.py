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
    from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
    from reyn.tools.drop_source import DROP_SOURCE

    # Wave 2 additions (ADR-0026 M3 Wave 2)
    from reyn.tools.file import DELETE_FILE, FILE_OP, LIST_DIRECTORY, READ_FILE, WRITE_FILE
    from reyn.tools.invoke_skill import INVOKE_SKILL, RUN_SKILL_OP
    from reyn.tools.lint import LINT
    from reyn.tools.mcp import (
        CALL_MCP_TOOL,
        DESCRIBE_MCP_TOOL,
        LIST_MCP_SERVERS,
        LIST_MCP_TOOLS,
        MCP_OP,
    )
    from reyn.tools.mcp_install import MCP_INSTALL_OP
    from reyn.tools.memory import (
        FORGET_MEMORY,
        LIST_MEMORY,
        READ_MEMORY_BODY,
        REMEMBER_AGENT,
        REMEMBER_SHARED,
    )
    from reyn.tools.plan import PLAN
    from reyn.tools.recall import RECALL
    from reyn.tools.reyn_src import REYN_SRC_LIST, REYN_SRC_READ
    from reyn.tools.shell import SHELL
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
    registry.register(INVOKE_SKILL)
    # RAG ops (ADR-0033 Phase 1)
    registry.register(RECALL)
    registry.register(DROP_SOURCE)
    # File ops (Wave 2 — Open Q #6 fine-grained naming)
    registry.register(READ_FILE)
    registry.register(WRITE_FILE)
    registry.register(DELETE_FILE)
    registry.register(LIST_DIRECTORY)
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
    # ── Phase-only capabilities (gates.router=deny, gates.phase=allow) ──
    registry.register(SHELL)
    registry.register(LINT)
    registry.register(ASK_USER)
    # ── Router-only capabilities (gates.router=allow, gates.phase=deny) ──
    registry.register(DELEGATE_TO_AGENT)
    registry.register(PLAN)
    registry.register(REYN_SRC_LIST)
    registry.register(REYN_SRC_READ)
    # ── Phase-only coarse-name ops (gates.router=deny, gates.phase=allow) ─
    # ADR-0026 Phase 4: Control IR ``kind: file/mcp/run_skill`` values map
    # 1:1 to these coarse ToolDefinitions; ControlIRExecutor dispatches via
    # the registry by op.kind name.  Router-side stays on the fine-grained
    # equivalents (read_file/write_file/etc., call_mcp_tool, invoke_skill).
    registry.register(FILE_OP)
    registry.register(MCP_OP)
    registry.register(RUN_SKILL_OP)
    registry.register(MCP_INSTALL_OP)
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
