"""prompt_toolkit-based REPL for ChatSession."""
from __future__ import annotations
import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from .session import ChatSession


_PREFIX = {
    "agent": "agent>",
    "status": "[…]",
    "ask": "[ask]",
    "trace": "[trace]",
    "skill_done": "[done]",
    "error": "[error]",
}


async def _input_loop(session: ChatSession, prompt_session: PromptSession) -> None:
    while True:
        try:
            with patch_stdout():
                text = await prompt_session.prompt_async("you > ")
        except (EOFError, KeyboardInterrupt):
            await session.shutdown()
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            await session.shutdown()
            return
        if text == "/remember":
            await session.trigger_manual_extraction()
            continue
        await session.submit_user_text(text)


async def _output_loop(session: ChatSession) -> None:
    while True:
        kind, text = await session.outbox.get()
        if kind == "__end__":
            return
        prefix = _PREFIX.get(kind, "")
        if prefix:
            print(f"{prefix} {text}")
        else:
            print(text)
        sys.stdout.flush()


async def run_repl(session: ChatSession) -> None:
    history_path = session.workspace_dir / ".input_history"
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))

    print(f"reyn chat — chat_id={session.chat_id}")
    print("Type /quit or Ctrl-D to exit.")
    print()

    runner = asyncio.create_task(session.run())
    inputs = asyncio.create_task(_input_loop(session, prompt_session))
    outputs = asyncio.create_task(_output_loop(session))

    try:
        await runner
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)
