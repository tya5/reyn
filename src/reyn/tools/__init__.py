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
    ToolHandler,
    ToolResult,
)
from reyn.tools.registry import ToolRegistry

__all__ = [
    "ToolDefinition",
    "ToolGates",
    "ToolContext",
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
    from reyn.tools.shell import SHELL
    from reyn.tools.lint import LINT
    from reyn.tools.ask_user import ASK_USER
    from reyn.tools.delegate_to_agent import DELEGATE_TO_AGENT
    from reyn.tools.plan import PLAN
    from reyn.tools.reyn_src import REYN_SRC_LIST, REYN_SRC_READ

    registry = ToolRegistry()
    # Both-surface capabilities
    registry.register(WEB_SEARCH)
    registry.register(WEB_FETCH)
    # Phase-only capabilities (= gates.router == "deny")
    registry.register(SHELL)
    registry.register(LINT)
    registry.register(ASK_USER)
    # Router-only capabilities (= gates.phase == "deny")
    registry.register(DELEGATE_TO_AGENT)
    registry.register(PLAN)
    registry.register(REYN_SRC_LIST)
    registry.register(REYN_SRC_READ)
    return registry
