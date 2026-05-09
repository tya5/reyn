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

    registry = ToolRegistry()
    registry.register(WEB_SEARCH)
    return registry
