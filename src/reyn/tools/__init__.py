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
]
