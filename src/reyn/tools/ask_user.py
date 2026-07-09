"""ask_user ToolDefinition — ADR-0026 M3 Wave 1.

Phase-only capability: gates.router="deny", gates.phase="allow".
The existing handler in src/reyn/op_runtime/ask_user.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx) signature and the new (args, ctx) signature.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

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
    existing (op, ctx) signature.

    ask_user is gated router="deny". With the phase-dispatch path removed
    (#2542), no live dispatcher reaches this ToolDefinition handler — the
    live user-intervention feature runs through OpContext.intervention_bus
    (InterventionHandler / InterventionBus), not through this tool. This
    handler is retained but formally unreachable; if it is ever invoked, it
    passes ctx=None to the op handler, which raises the documented
    RuntimeError (ask_user requires an intervention_bus on OpContext).
    """
    # Lazy import to avoid circular dependency at registry-init time.
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

    return await handle(op=op, ctx=None)


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

ASK_USER = ToolDefinition(
    canonical=STRUCTURED_PASSTHROUGH,
    name="ask_user",
    description=_ASK_USER_DESCRIPTION,
    parameters=_ASK_USER_PARAMETERS,
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle,
    category="interactive",
    purity="side_effect",   # produces UserIntervention; modifies intervention queue
)
