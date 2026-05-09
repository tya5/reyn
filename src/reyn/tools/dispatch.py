"""Dispatch helpers for the unified tool registry (ADR-0026 M1).

The protocol-specific dispatchers (RouterLoop / ControlIRExecutor)
each use these helpers to look up tools, build ToolContext, and
invoke handlers consistently. M1 keeps dispatch shape minimal;
M3 may extend as capability migrations surface needs.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.registry import ToolRegistry
from reyn.tools.types import ToolContext, ToolDefinition, ToolResult


class ToolNotFound(KeyError):
    """Raised when a dispatcher receives a tool name not in the registry."""


class ToolGateRefused(PermissionError):
    """Raised when a dispatcher's protocol gate denies the tool.

    Layer 1 (= role gate) refusal. Distinct from layer 3 (= permission
    resolver) refusal which is per-call.
    """


async def invoke_tool(
    registry: ToolRegistry,
    name: str,
    args: Mapping[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """Look up + invoke a tool. Caller (= dispatcher) is responsible
    for verifying gates BEFORE this call (= the dispatcher knows its
    own protocol; this helper is post-gate). Args are passed unmodified;
    validation is the caller's responsibility (= per-protocol strictness
    choice — see ADR-0026 §6 / Open Question on Pydantic vs JSON schema).
    """
    tool = registry.lookup(name)
    if tool is None:
        raise ToolNotFound(name)
    return await tool.handler(args, ctx)
