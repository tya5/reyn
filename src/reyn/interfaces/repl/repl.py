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

import logging
import sys

from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.registry import AgentRegistry  # #312 PR-A: registry stays in the runtime pkg

from .client_driver import run_chat_client
from .read_model import RegistryReadModel
from .renderer import ChatRenderer

logger = logging.getLogger(__name__)


async def run_repl(registry: AgentRegistry, renderer: ChatRenderer, *, config=None) -> None:
    """Attach to the default agent (or pre-attached one) and run the REPL.

    Caller is expected to have called `await registry.attach(name)` before
    invoking this function so the user lands on a known agent.

    ``config`` is the loaded ReynConfig (or None). When supplied it is threaded
    read-only to ``run_inline_input`` so the ``…`` overflow chip can surface
    cron / mcp / hooks state. The --cui / non-TTY path is not affected.

    ADR-0039 P3: the LOCAL half of the unified chat client — it constructs the
    transport-specific pair (an :class:`InProcessTransport` + a
    :class:`RegistryReadModel`) and hands off to the SHARED
    :func:`~reyn.interfaces.repl.client_driver.run_chat_client` driver, which owns
    the banner + renderer-loop selection + output loop identically for local and
    remote. Only the transport lifecycle and the cost summary stay here.
    """
    attached = registry.attached_session()
    if attached is None:
        raise RuntimeError("run_repl requires an attached agent; call registry.attach() first")

    # The transport is the client's sole seam to the session. It composes the
    # two pre-existing render paths behind ONE unified frame stream:
    #  - the display outbox (session.outbox → forwarder → repl_outbox), and
    #  - the renderer's working-indicator chat-event subset (turn_started →
    #    spinner, turn_settled → idle, tool_called → Running <tool>, …).
    # `start()` binds the focus-following chat-event subscription + the
    # intervention listener channel (so ask_user / cost-warn / permission
    # prompts surface and can be answered — the session is built with
    # enforce_listener_presence=True, so an unregistered listener silently
    # auto-refuses; DEFAULT_CHAT_CHANNEL_ID ("tui") names the channel),
    # and starts the outbox → frame pump. The binding follows the FOCUSED
    # session across `/attach` (re-wired by the registry), so neither the
    # working indicator nor the intervention channel strands on the old session.
    from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
    transport = InProcessTransport(
        registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID
    )
    transport.start()

    # The LOCAL read-model reads status/region/tasks off the attached session —
    # byte-identical to the pre-P3 inline reads.
    read_model = RegistryReadModel(registry)

    try:
        await run_chat_client(
            transport=transport,
            renderer=renderer,
            read_model=read_model,
            agent_name=attached.agent_name,
            is_tty=sys.stdin.isatty(),
            config=config,
        )
    finally:
        # Unwire the transport from the LIVE attached session (handles a switch
        # before quit) and stop the frame pump.
        transport.close()
        from reyn.llm.pricing import TokenUsage
        total_usage = TokenUsage()
        total_cost = 0.0
        for name in registry.loaded_names():
            total_cost += registry.agent_cost_usd(name)
            total_usage += registry.agent_total_usage(name)
        renderer.cost_summary(total_usage, total_cost if total_cost > 0 else None)
