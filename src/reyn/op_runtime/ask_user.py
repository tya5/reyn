"""ask_user kind handler — pause and ask the user a clarifying question.

This op is control-IR-only; the dispatcher in op_runtime/__init__.py rejects
preprocessor invocations before they reach this handler.
"""
from __future__ import annotations
import asyncio
import inspect
from typing import Literal

from . import register
from .context import OpContext
from ..models import AskUserIROp


async def _default_user_input(question: str, suggestions: list[str]) -> str:
    """Default ask_user backend.

    Uses prompt_toolkit's PromptSession for native asyncio integration when
    a TTY is available; falls back to running blocking input() in a thread.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        session: PromptSession[str] = PromptSession()
        with patch_stdout():
            text = await session.prompt_async("  > ")
        return (text or "").strip()
    except Exception:
        return (await asyncio.to_thread(_blocking_prompt)).strip()


def _blocking_prompt() -> str:
    print("  > ", end="", flush=True)
    return input()


async def handle(op: AskUserIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    ctx.events.emit(
        "user_intervention_requested",
        phase=ctx.current_phase,
        question=op.question,
        suggestions=op.suggestions or [],
    )

    user_input_fn = ctx.user_input_fn or _default_user_input
    result = user_input_fn(op.question, op.suggestions or [])
    text = await result if inspect.isawaitable(result) else result
    if not text and not op.required:
        text = ""

    ctx.events.emit("user_intervention_received", phase=ctx.current_phase, answer=text)
    return {"kind": "ask_user", "question": op.question, "answer": text, "status": "ok"}


register("ask_user", handle)
