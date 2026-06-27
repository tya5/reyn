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
from reyn.user_intervention import InterventionChoice, UserIntervention

from . import register
from .context import OpContext


def _options_to_choices(options: list[str]) -> list[InterventionChoice]:
    """Pure: map free-text ask_user options to selectable choices.

    ``id`` is the option text (so the answer is the option itself), ``label`` is
    ``[N] <option>``, and ``hotkey`` is the 1-based number — the stdin / --cui
    path types the number, the inline region selects with ↑↓.
    """
    return [
        InterventionChoice(id=opt, label=f"[{i + 1}] {opt}", hotkey=str(i + 1))
        for i, opt in enumerate(options)
    ]


async def handle(op: AskUserIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    if ctx.intervention_bus is None:
        raise RuntimeError(
            "ask_user invoked without an intervention_bus on OpContext. "
            "Wire a bus (StdinInterventionBus for CLI, ChatInterventionBus "
            "for chat) when constructing the SkillRuntime."
        )

    choices = _options_to_choices(op.options or [])
    iv = UserIntervention(
        kind="ask_user",
        prompt=op.question,
        suggestions=op.suggestions or [],
        choices=choices,
        input_type="select" if choices else "",
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
        options=op.options or [],
    )

    answer = await ctx.intervention_bus.request(iv)
    # A selected option resolves with choice_id (= the option text); free-text
    # resolves with text.
    text = answer.choice_id or answer.text or ""
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
