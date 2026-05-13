"""ask_user kind handler — pause and ask the user a clarifying question.

This op is control-IR-only; the dispatcher in op_runtime/__init__.py rejects
preprocessor invocations before they reach this handler.

The op routes through `ctx.intervention_bus`. The chat REPL wires a
`ChatInterventionBus`; the CLI wires a `StdinInterventionBus`. Either way
the handler emits a free-text `UserIntervention` and awaits the answer.
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import AskUserIROp
from reyn.user_intervention import UserIntervention

from . import register
from .context import OpContext


async def handle(op: AskUserIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    if ctx.intervention_bus is None:
        raise RuntimeError(
            "ask_user invoked without an intervention_bus on OpContext. "
            "Wire a bus (StdinInterventionBus for CLI, ChatInterventionBus "
            "for chat) when constructing the Agent."
        )

    iv = UserIntervention(
        kind="ask_user",
        prompt=op.question,
        suggestions=op.suggestions or [],
        skill_name=ctx.skill_name or None,
        run_id=None,  # set by chat session if it tracks runs; CLI ignores
    )

    ctx.events.emit(
        "user_intervention_requested",
        run_id=ctx.run_id,
        skill=ctx.skill_name,
        phase=ctx.current_phase,
        question=op.question,
        intervention_id=iv.id,
        suggestions=op.suggestions or [],
    )

    answer = await ctx.intervention_bus.request(iv)
    text = answer.text or ""
    if not text and not op.required:
        text = ""

    ctx.events.emit(
        "user_intervention_received",
        run_id=ctx.run_id,
        skill=ctx.skill_name,
        phase=ctx.current_phase,
        answer=text,
        intervention_id=iv.id,
    )
    return {"kind": "ask_user", "question": op.question, "answer": text, "status": "ok"}


register("ask_user", handle)
