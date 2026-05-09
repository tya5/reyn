"""ask_user ToolDefinition — ADR-0026 M3 Wave 1.

Phase-only capability: gates.router="deny", gates.phase="allow".
The existing handler in src/reyn/op_runtime/ask_user.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx, caller) signature and the new (args, ctx) signature.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


_ASK_USER_DESCRIPTION = (
    "Pause the current phase and ask the user a clarifying question. "
    "The OS suspends execution, presents the question (and optional "
    "suggestions) to the user, waits for a free-text answer, and "
    "resumes the phase with the answer available as a control IR result. "
    "question: the question to display to the user. "
    "suggestions: optional list of suggested responses. "
    "required: if true (default), an empty answer is rejected."
)

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
    """Adapter wrapping op_runtime.ask_user.handle.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature. Once M3 completes, the
    body of handle may be inlined here in M4 cleanup.

    ask_user is phase-only (gates.router="deny"), so ctx.caller_kind
    is always "phase" here. The intervention_bus is retrieved from
    ctx.phase_state.op_context (a PhaseCallerState holding the OpContext)
    which is wired by the phase dispatcher before invoking this handler
    (M4 Phase 3).
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.ask_user import handle
    from reyn.schemas.models import AskUserIROp

    # Build a transient AskUserIROp from args (= reuse Pydantic
    # validation that the existing op handler expects).
    op = AskUserIROp(
        kind="ask_user",
        question=args["question"],
        suggestions=list(args.get("suggestions", [])),
        required=bool(args.get("required", True)),
    )

    # ctx.phase_state.op_context is the OpContext built by the phase dispatcher
    # (M4 Phase 3 wiring). ask_user requires intervention_bus on OpContext; if
    # op_context is not populated (e.g. in unit tests that pass a stub or before
    # M4 Phase 3 wires the dispatcher), the handler will raise RuntimeError as designed.
    legacy_ctx = ctx.phase_state.op_context if ctx.phase_state is not None else None

    return await handle(op=op, ctx=legacy_ctx, caller="control_ir")


ASK_USER = ToolDefinition(
    name="ask_user",
    description=_ASK_USER_DESCRIPTION,
    parameters=_ASK_USER_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle,
    category="interactive",
    purity="side_effect",   # produces UserIntervention; modifies intervention queue
)
