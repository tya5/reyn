"""The shared chat-client driver — ONE renderer/loop layer for local AND remote.

ADR-0039 D2 is *local ≡ remote by construction*. P1/P2 unified the transport
(:class:`~reyn.interfaces.transport.client_transport.ClientTransport`, with an
in-process and an AG-UI sibling) and the frame stream; this module unifies the
last divergent layer — the **renderer selection + input/output loops**. Before
P3, ``run_repl`` (local) and ``run_remote_repl`` (remote) each hand-rolled: pick
inline-vs-console, banner, select the inline Application vs the PromptSession
loop, run the shared output loop, wait, tear down. The remote copy silently
dropped the inline branch, so ``reyn chat --connect`` on a TTY rendered the plain
CUI while local rendered the Rich inline TUI — the D2 gap the owner hit.

:func:`run_chat_client` is that shared body. Both call sites now construct only
the transport-specific pair — a :class:`ClientTransport` and a
:class:`~reyn.interfaces.repl.read_model.ChatReadModel` — and hand off here. The
renderer is chosen ONCE (by the caller, via the same
``logger_factory.make_renderer`` predicate) and the input driver is selected ONCE
(here), so the TUI is agnostic to whether its session is a page-fault away or a
network away.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .stream_client import run_input_loop, run_output_loop

if TYPE_CHECKING:
    from reyn.interfaces.transport.client_transport import ClientTransport

    from .read_model import ChatReadModel
    from .renderer import ChatRenderer

logger = logging.getLogger(__name__)


async def run_chat_client(
    *,
    transport: "ClientTransport",
    renderer: "ChatRenderer",
    read_model: "ChatReadModel",
    agent_name: str,
    is_tty: bool,
    config=None,
) -> None:
    """Banner + input-driver selection + output loop + wait + teardown.

    ``renderer.uses_app_input()`` AND ``is_tty`` selects the interactive inline
    Application (its own rule-bar input, reading status/region/tasks through
    ``read_model``); otherwise the plain PromptSession loop (``--cui`` / non-TTY /
    piped). The output loop is shared. This is the SAME selection ``run_repl``
    made locally — now applied identically to the remote path.

    The caller owns transport lifecycle (``start``/``close`` or the httpx SSE
    context) and any cost summary; this function only drives the two loops and
    guarantees both are cancelled + awaited on exit.
    """
    renderer.banner(agent_name)

    # `set` = "no reply pending" (the PromptSession input loop's pacing gate is
    # open); `clear` = "a turn is in flight". The inline Application path does not
    # use it (it has its own submit flow), but the shared output loop always
    # signals it so the plain-path gate never hangs.
    reply_seen: asyncio.Event = asyncio.Event()
    reply_seen.set()

    if renderer.uses_app_input() and is_tty:
        from reyn.interfaces.inline.app import run_inline_input  # noqa: PLC0415
        inputs = asyncio.create_task(
            run_inline_input(read_model, renderer, config, transport)
        )
    else:
        from prompt_toolkit import PromptSession  # noqa: PLC0415
        from prompt_toolkit.history import FileHistory  # noqa: PLC0415
        from prompt_toolkit.styles import Style  # noqa: PLC0415
        prompt_session: "PromptSession[str]" = PromptSession(
            history=FileHistory(str(read_model.history_path)),
            style=Style.from_dict({"bottom-toolbar": "noreverse bg:default"}),
        )
        inputs = asyncio.create_task(
            run_input_loop(transport, prompt_session, renderer, reply_seen)
        )

    outputs = asyncio.create_task(
        run_output_loop(
            transport, renderer, reply_seen,
            command_ui_region=read_model.has_command_ui_region,
        )
    )

    try:
        await asyncio.wait({inputs, outputs}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)


__all__ = ["run_chat_client"]
