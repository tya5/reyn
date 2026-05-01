"""prompt_toolkit-based REPL for ChatSession."""
from __future__ import annotations
import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from .renderer import ChatRenderer
from .session import ChatSession


async def _input_loop(
    session: ChatSession,
    prompt_session: PromptSession,
    renderer: ChatRenderer,
) -> None:
    while True:
        try:
            with patch_stdout():
                text = await prompt_session.prompt_async(renderer.prompt_text())
        except (EOFError, KeyboardInterrupt):
            await session.shutdown()
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            await session.shutdown()
            return
        await session.submit_user_text(text)


async def _output_loop(session: ChatSession, renderer: ChatRenderer) -> None:
    while True:
        msg = await session.outbox.get()
        if msg.kind == "__end__":
            return
        # Wrap in run_in_terminal so the prompt is cleared before output and
        # redrawn after — required for ANSI/Rich to render cleanly without
        # corrupting the prompt. When no app is active (banner phase), the
        # function runs synchronously without coordination.
        if get_app_or_none() is not None:
            await run_in_terminal(lambda m=msg: renderer.message(m))
        else:
            renderer.message(msg)


async def run_repl(session: ChatSession, renderer: ChatRenderer) -> None:
    history_path = session.workspace_dir / ".input_history"
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))

    renderer.banner(session.chat_id)

    runner = asyncio.create_task(session.run())
    inputs = asyncio.create_task(_input_loop(session, prompt_session, renderer))
    outputs = asyncio.create_task(_output_loop(session, renderer))

    try:
        await runner
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)
        cost_usd = session.total_cost_usd if session.total_cost_usd > 0 else None
        renderer.cost_summary(session.total_usage, cost_usd)
