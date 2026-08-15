"""prompt_toolkit-based REPL for AgentRegistry-managed multi-agent chat.

``run_repl`` is the composition root of the chat client (ADR-0039 P1): it
constructs the :class:`~reyn.interfaces.transport.in_process.InProcessTransport`
from the registry and wires the stream-consuming client
(:mod:`reyn.interfaces.repl.stream_client`) to it. The client then consumes ONE
unified frame stream (display outbox + the renderer-relevant audit-event subset)
and routes user input back through the transport's send side, so a local run
exercises the same client path a remote client (P2) will.

Agent switching (`/attach <name>`) flips the registry's attached pointer; the
transport's focus binding re-wires the audit-event subscription across the
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


async def run_repl(
    registry: AgentRegistry, renderer: ChatRenderer, *, name: str, config=None
) -> None:
    """Run the REPL against ``registry``, targeting agent ``name``.

    #3671 P2: the caller no longer has to attach before calling this — the
    client is allowed to render (and the user to see the shell) while
    ``registry.attach(name)`` is still running in the background (restoring
    WAL-derived state can take a while for a project with many in-flight
    agents). Every seam below already tolerated an unattached registry
    (``InProcessTransport``'s accessors guard on ``_attached() is None``,
    ``_wire_focus_listeners(None)`` is a no-op, ``RegistryReadModel.snapshot``
    returns ``None`` with nothing attached) — ``name`` (the caller's intended
    target, known before attach can possibly succeed) replaces the
    now-removed hard requirement that an attached :class:`Session` already
    exist, purely for the banner / Textual app's own display label.

    #4824: this claim was NOT true for one seam — ``RegistryReadModel.
    history_path`` hard-raised on an unattached registry instead of
    tolerating it, and a piped/non-TTY ``reyn chat`` invocation reaches it
    with essentially no ``await`` between ``attach()``'s ``create_task`` and
    the ``FileHistory(...)`` construction that reads it, so the crash fired
    on ~every such invocation, not occasionally. Fixed by giving
    ``RegistryReadModel`` the SAME ``name`` this function already has
    (below), so ``history_path`` can fall back to
    :meth:`~reyn.runtime.registry.AgentRegistry.agent_workspace_dir` — a
    pure path derivation needing no live :class:`Session`, so it costs
    nothing and races nothing. The claim above is accurate again.

    ``config`` is the loaded ReynConfig (or None). When supplied it is threaded
    read-only to the status snapshot (``interfaces/repl/status.py``'s
    ``_snapshot``) so the ``…`` overflow chip can surface cron / mcp / hooks
    state. The --cui / non-TTY path is not affected.

    ADR-0039 P3: the LOCAL half of the unified chat client — it constructs the
    transport-specific pair (an :class:`InProcessTransport` + a
    :class:`RegistryReadModel`) and hands off to the SHARED
    :func:`~reyn.interfaces.repl.client_driver.run_chat_client` driver, which owns
    the banner + renderer-loop selection + output loop identically for local and
    remote. Only the transport lifecycle and the cost summary stay here.
    """
    # The transport is the client's sole seam to the session. It composes the
    # two pre-existing render paths behind ONE unified frame stream:
    #  - the display outbox (session.outbox → forwarder → repl_outbox), and
    #  - the renderer's working-indicator audit-event subset (turn_started →
    #    spinner, turn_settled → idle, tool_called → Running <tool>, …).
    # `start()` binds the focus-following audit-event subscription + the
    # intervention listener channel (so ask_user / cost-warn / permission
    # prompts surface and can be answered — the session is built with
    # enforce_listener_presence=True, so an unregistered listener silently
    # auto-refuses; DEFAULT_CHAT_CHANNEL_ID ("tui") names the channel),
    # and starts the outbox → frame pump. The binding follows the FOCUSED
    # session across `/attach` (re-wired by the registry), so neither the
    # working indicator nor the intervention channel strands on the old session.
    from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
    from reyn.runtime.startup_timing import stage  # noqa: PLC0415

    with stage("client-prep:transport"):
        transport = InProcessTransport(
            registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID
        )
        transport.start()

    # The LOCAL read-model reads status/region/tasks off the attached session —
    # byte-identical to the pre-P3 inline reads. ``agent_name=name`` (#4824):
    # the caller's intended target, known before attach can possibly have
    # succeeded — lets ``RegistryReadModel.history_path`` tolerate the startup
    # race window this function's own docstring already promises every OTHER
    # seam tolerates.
    with stage("client-prep:read-model"):
        read_model = RegistryReadModel(registry, agent_name=name)

    try:
        await run_chat_client(
            transport=transport,
            renderer=renderer,
            read_model=read_model,
            agent_name=name,
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
