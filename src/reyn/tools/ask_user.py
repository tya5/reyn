"""ask_user ToolDefinition — ADR-0026 M3 Wave 1.

Phase-only capability: gates.router="deny", gates.phase="allow".
The existing handler in src/reyn/op_runtime/ask_user.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx) signature and the new (args, ctx) signature.

#2708 P3.2a: the adapter now rebuilds the real OpContext from
``ctx.router_state.op_context_factory()`` (the ``tools/present.py`` precedent) so a
pipeline ``tool: ask_user`` step reaches the session-wired ``intervention_bus`` — and,
for a chat-invoked pipeline's attached driver-session, the parent-bound bridge that
delivers the ask to the live operator (the fix for #2721's silent auto-refuse).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import interactive as _interactive_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Relocated to reyn.tools.descriptions.interactive (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_ASK_USER_DESCRIPTION = _interactive_descriptions.ask_user.text

_ASK_USER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "required": {"type": "boolean"},
    },
    "required": ["question"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the ask_user op via op_runtime, through the REAL OpContext.

    Builds an ``AskUserIROp`` from the tool args and calls the registered ask_user
    handler with the real ``OpContext`` from ``ctx.router_state``'s factory — the
    ``present.py`` precedent (the present-layer arc's ``tool: present`` enabler). The
    real OpContext carries the session-wired ``intervention_bus`` (built by the
    session's ``make_router_op_context``), so a pipeline ``tool: ask_user`` step reaches
    the live user-intervention machinery instead of raising.

    #2708 P3.2a: for a chat-invoked pipeline's ATTACHED driver-session, that
    ``intervention_bus`` is bound (by the ``SpawnBridgeInterventionListener`` spawn
    override) to the PARENT session's live-operator listener — so the driver's ask_user
    reaches the operator blocked on the parent, instead of silently auto-refusing (#2721).
    When there is no router_state factory (a bare/test ToolContext), we fall back to
    ctx=None, whose op handler raises the documented "requires an intervention_bus"
    RuntimeError — the same fail-fast as before this rebuild, never a silent wrong answer.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.ask_user import handle
    from reyn.schemas.models import AskUserIROp

    # Build a transient AskUserIROp from args (= reuse Pydantic
    # validation that the existing op handler expects).
    op = AskUserIROp(
        kind="ask_user",
        question=args["question"],
        suggestions=list(args.get("suggestions", [])),
        required=bool(args.get("required", True)),
    )

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
        return await execute_op(op, legacy_ctx)

    # No router-state OpContext factory (bare/test ToolContext): ctx=None → the op
    # handler raises its documented "ask_user requires an intervention_bus" error.
    return await handle(op=op, ctx=None)


from reyn.core.offload.canonical import ask_user_to_canonical  # noqa: E402

ASK_USER = ToolDefinition(
    canonical=ask_user_to_canonical,
    name="ask_user",
    description=_ASK_USER_DESCRIPTION,
    parameters=_ASK_USER_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle,
    category="interactive",
    purity="side_effect",   # produces UserIntervention; modifies intervention queue
)
