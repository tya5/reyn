"""Reyn unified tool registry — single source of truth for capabilities
exposed to both router-style (function calling) and phase-style
(Control IR JSON output) LLM invocations.

Per ADR-0026 (Status: Proposed). M1 lays the infrastructure;
capability migrations land in M2/M3.
"""
from reyn.tools.types import (
    ToolDefinition,
    ToolGates,
    ToolContext,
    RouterCallerState,
    PhaseCallerState,
    ToolHandler,
    ToolResult,
)
from reyn.tools.registry import ToolRegistry

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
    from reyn.tools.web_search import WEB_SEARCH
    from reyn.tools.web_fetch import WEB_FETCH
    from reyn.tools.invoke_skill import INVOKE_SKILL
    from reyn.tools.shell import SHELL
    from reyn.tools.lint import LINT
    from reyn.tools.ask_user import ASK_USER
    from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
    from reyn.tools.plan import PLAN
    from reyn.tools.reyn_src import REYN_SRC_LIST, REYN_SRC_READ
    # Wave 2 additions (ADR-0026 M3 Wave 2)
    from reyn.tools.file import READ_FILE, WRITE_FILE, DELETE_FILE, LIST_DIRECTORY
    from reyn.tools.mcp import CALL_MCP_TOOL, LIST_MCP_SERVERS, LIST_MCP_TOOLS
    from reyn.tools.memory import (
        LIST_MEMORY,
        READ_MEMORY_BODY,
        REMEMBER_SHARED,
        REMEMBER_AGENT,
        FORGET_MEMORY,
    )
    from reyn.tools.catalog import (
        LIST_SKILLS,
        DESCRIBE_SKILL,
        LIST_AGENTS,
        DESCRIBE_AGENT,
    )

    registry = ToolRegistry()
    # ── Both-surface capabilities (gates.router=allow, gates.phase=allow) ──
    registry.register(WEB_SEARCH)
    registry.register(WEB_FETCH)
    registry.register(INVOKE_SKILL)
    # File ops (Wave 2 — Open Q #6 fine-grained naming)
    registry.register(READ_FILE)
    registry.register(WRITE_FILE)
    registry.register(DELETE_FILE)
    registry.register(LIST_DIRECTORY)
    # MCP ops (Wave 2 — Type C closure: phase-side discover)
    registry.register(CALL_MCP_TOOL)
    registry.register(LIST_MCP_SERVERS)
    registry.register(LIST_MCP_TOOLS)
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
    return registry
