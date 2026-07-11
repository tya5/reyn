"""prompt_toolkit-based REPL for AgentRegistry-managed multi-agent chat.

``run_repl`` is the composition root of the chat client (ADR-0039 P1): it
constructs the :class:`~reyn.interfaces.transport.in_process.InProcessTransport`
from the registry and wires the stream-consuming client
(:mod:`reyn.interfaces.repl.stream_client`) to it. The client then consumes ONE
unified frame stream (display outbox + the renderer-relevant chat-event subset)
and routes user input back through the transport's send side, so a local run
exercises the same client path a remote client (P2) will.

Agent switching (`/attach <name>`) flips the registry's attached pointer; the
transport's focus binding re-wires the chat-event subscription across the
switch, and both the input and output sides funnel through the registry-owned
``repl_outbox`` the transport drains.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.registry import AgentRegistry  # #312 PR-A: registry stays in the runtime pkg

from .renderer import ChatRenderer
from .stream_client import run_input_loop, run_output_loop

logger = logging.getLogger(__name__)


async def run_repl(registry: AgentRegistry, renderer: ChatRenderer, *, config=None) -> None:
    """Attach to the default agent (or pre-attached one) and run the REPL.

    Caller is expected to have called `await registry.attach(name)` before
    invoking this function so the user lands on a known agent.

    ``config`` is the loaded ReynConfig (or None). When supplied it is threaded
    read-only to ``run_inline_input`` so the ``…`` overflow chip can surface
    cron / mcp / hooks state. The --cui / non-TTY path is not affected (it uses
    ``run_input_loop`` and never receives ``config``).
    """
    attached = registry.attached_session()
    if attached is None:
        raise RuntimeError("run_repl requires an attached agent; call registry.attach() first")

    history_path = attached.workspace_dir / ".input_history"

    # The transport is the client's sole seam to the session. It composes the
    # two pre-existing render paths behind ONE unified frame stream:
    #  - the display outbox (session.outbox → forwarder → repl_outbox), and
    #  - the renderer's working-indicator chat-event subset (turn_started →
    #    spinner, turn_settled → idle, tool_called → Running <tool>, …).
    # `start()` binds the focus-following chat-event subscription + the
    # intervention listener channel (so ask_user / cost-warn / permission
    # prompts surface and can be answered — the session is built with
    # enforce_listener_presence=True, so an unregistered listener silently
    # auto-refuses; DEFAULT_CHAT_CHANNEL_ID ("tui") mirrors the chainlit mount),
    # and starts the outbox → frame pump. The binding follows the FOCUSED
    # session across `/attach` (re-wired by the registry), so neither the
    # working indicator nor the intervention channel strands on the old session.
    from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
    transport = InProcessTransport(
        registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID
    )
    transport.start()

    renderer.banner(attached.agent_name)

    # `set` = "no reply pending" (the input loop's pacing gate is open).
    # `clear` = "a turn is in flight" (the gate blocks until the output
    # loop renders the reply). Start opened so the first read isn't gated.
    reply_seen: asyncio.Event = asyncio.Event()
    reply_seen.set()

    # Interactive TTY inline renderer → its own rule-bar Application input
    # driver. --cui / non-TTY (pipe / script) keep the PromptSession input loop
    # (plain invariance + the piped reply_seen pacing). The output loop is
    # shared: run_in_terminal prints above whichever input is live.
    if renderer.uses_app_input() and sys.stdin.isatty():
        from reyn.interfaces.inline.app import run_inline_input
        inputs = asyncio.create_task(run_inline_input(registry, renderer, config, transport))
    else:
        from prompt_toolkit.styles import Style
        prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            # Working-indicator toolbar as a dim status line, not the default
            # heavy reversed bar.
            style=Style.from_dict({"bottom-toolbar": "noreverse bg:default"}),
        )
        inputs = asyncio.create_task(
            run_input_loop(transport, prompt_session, renderer, reply_seen)
        )
    outputs = asyncio.create_task(
        run_output_loop(transport, renderer, reply_seen)
    )

    try:
        # Wait until one of the loops returns (user `/quit` or EOF).
        await asyncio.wait(
            {inputs, outputs}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Unwire the transport from the LIVE attached session (handles a switch
        # before quit) and stop the frame pump.
        transport.close()
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)
        from reyn.llm.pricing import TokenUsage
        total_usage = TokenUsage()
        total_cost = 0.0
        for name in registry.loaded_names():
            total_cost += registry.agent_cost_usd(name)
            total_usage += registry.agent_total_usage(name)
        renderer.cost_summary(total_usage, total_cost if total_cost > 0 else None)
