"""prompt_toolkit-based REPL for AgentRegistry-managed multi-agent chat.

The REPL drains the registry-owned `repl_outbox` (always present, regardless
of which agent is attached) and forwards user input to whichever agent is
currently attached. Agent switching (`:attach <name>`) flips the registry's
attached pointer; the REPL doesn't need to re-bind anything because both
the input and output sides funnel through the registry.
"""
from __future__ import annotations
import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from .renderer import ChatRenderer
from .registry import AgentRegistry


async def _input_loop(
    registry: AgentRegistry,
    prompt_session: PromptSession,
    renderer: ChatRenderer,
) -> None:
    is_tty = sys.stdin.isatty()
    while True:
        try:
            if is_tty:
                with patch_stdout():
                    text = await prompt_session.prompt_async(renderer.prompt_text())
            else:
                # Piped / scripted stdin: skip prompt_toolkit entirely. It
                # otherwise emits cursor-movement escapes (`\x1b[1A\x1b[K`)
                # that clutter logs and confuse line-buffered drivers.
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline,
                )
                if not line:
                    raise EOFError
                text = line
        except (EOFError, KeyboardInterrupt):
            await registry.shutdown()
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            await registry.shutdown()
            return
        attached = registry.attached_session()
        if attached is None:
            renderer.message(_simple_status("no agent attached; try :agents"))
            continue
        await attached.submit_user_text(text)


async def _output_loop(registry: AgentRegistry, renderer: ChatRenderer) -> None:
    is_tty = sys.stdout.isatty()
    while True:
        msg = await registry.repl_outbox.get()
        if msg.kind == "__end__":
            return
        # On a real terminal: wrap in run_in_terminal so the prompt is cleared
        # before output and redrawn after — required for ANSI/Rich to render
        # cleanly without corrupting the prompt.
        # On a pipe: print plainly, no prompt redraw, no cursor escapes.
        if is_tty and get_app_or_none() is not None:
            await run_in_terminal(lambda m=msg: renderer.message(m))
        else:
            renderer.message(msg)


def _simple_status(text: str):
    """Build a status OutboxMessage for inline rendering (no async needed)."""
    from .outbox import OutboxMessage
    return OutboxMessage(kind="status", text=text)


async def run_repl(registry: AgentRegistry, renderer: ChatRenderer) -> None:
    """Attach to the default agent (or pre-attached one) and run the REPL.

    Caller is expected to have called `await registry.attach(name)` before
    invoking this function so the user lands on a known agent.
    """
    attached = registry.attached_session()
    if attached is None:
        raise RuntimeError("run_repl requires an attached agent; call registry.attach() first")

    history_path = attached.workspace_dir / ".input_history"
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_path)))

    renderer.banner(attached.agent_name)

    inputs = asyncio.create_task(_input_loop(registry, prompt_session, renderer))
    outputs = asyncio.create_task(_output_loop(registry, renderer))

    try:
        # Wait until one of the loops returns (user `/quit` or EOF).
        await asyncio.wait(
            {inputs, outputs}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)
        # Aggregate cost from all loaded agents
        from reyn.llm.pricing import TokenUsage
        total_usage = TokenUsage()
        total_cost = 0.0
        for name in registry.loaded_names():
            session = registry._agents.get(name)
            if session is None:
                continue
            total_usage += session.total_usage
            total_cost += session.total_cost_usd
        renderer.cost_summary(total_usage, total_cost if total_cost > 0 else None)
