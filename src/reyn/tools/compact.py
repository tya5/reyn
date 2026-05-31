"""compact ToolDefinition (#272 / #1128) — voluntary history compaction.

Router- and phase-callable LLM entry point that lets the model voluntarily
compact the conversation/phase history when the OS-injected context-size signal
shows the window filling, instead of waiting for the mandatory retry_loop
backstop. The handler delegates to ``op_runtime.compact``, which routes to the
caller-wired ``OpContext.compact_now`` capability and returns the freed tokens +
free window afterwards in exact tokens.

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_COMPACT_DESCRIPTION = (
    "Compact the conversation history now: summarise older turns to free up "
    "context window. Use this when the 'Context window' status shows the free "
    "window getting low and you still have work to do — compacting first frees "
    "room so subsequent steps and large tool results fit. Returns the freed "
    "tokens and the free window afterwards (exact tokens). The system also "
    "compacts automatically as a backstop; this lets you do it proactively."
)

_COMPACT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": (
                "Optional short rationale for the audit trail (e.g. 'window "
                "low before reading large file'). Not interpreted by the OS."
            ),
        },
    },
    "required": [],
}


async def _handle_compact(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the compact op via op_runtime.

    Builds a CompactIROp and calls the registered compact handler with the
    OpContext from ctx.phase_state (phase-side) or ctx.router_state factory
    (router-side). The op handler errors cleanly if no compaction capability
    is wired, so this never silently no-ops.
    """
    from reyn.op_runtime import execute_op
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import CompactIROp

    reason = args.get("reason")
    op = CompactIROp(kind="compact", reason=str(reason) if reason else None)

    _op_ctx = ctx.phase_state.op_context if ctx.phase_state is not None else None
    if _op_ctx is not None and isinstance(_op_ctx, OpContext):
        legacy_ctx = _op_ctx
    elif (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context (no compaction capability) → the op handler returns
        # a clear compaction_unavailable error rather than a silent no-op.
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    return await execute_op(op, legacy_ctx, caller="control_ir")


COMPACT = ToolDefinition(
    name="compact",
    description=_COMPACT_DESCRIPTION,
    parameters=_COMPACT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_compact,
    category="context",
    purity="side_effect",
)
