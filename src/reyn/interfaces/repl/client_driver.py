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

The interactive TTY surface (``renderer.uses_app_input() and is_tty``) is the
Textual conversation-pane app (:mod:`reyn.interfaces.inline.textual_chat`), which
OWNS both input and output and consumes the same ``transport.frames()`` stream in
a worker — so it renders identically over a local or a remote transport. That app
module (and its ``textual`` / ``textual_flowview`` imports) is imported LAZILY
inside the branch, TTY-path only: the plain / ``--cui`` / non-TTY / CI paths take
the shared :class:`~reyn.interfaces.repl.renderer.ChatRenderer` + PromptSession
input loop below and never import flowview, so they stay green even if it is
absent.
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


def resolve_render_mode(mode: str, *, is_tty: bool) -> str:
    """Resolve a configured ``chat.render_mode`` + TTY presence to a concrete path.

    Returns one of ``"alt-screen"``, ``"inline"``, ``"plain"`` — the three
    physical render paths (the first two are the Textual conversation-pane app's
    driver modes; the last is the plain :class:`ChatRenderer`).

    The TTY guard is universal: a non-TTY session (piped, CI, sandbox with no
    real terminal, or a host where alt-screen would silently no-op) can never
    enter an interactive Textual driver, so it always resolves to ``"plain"``
    regardless of the configured mode. On a TTY: ``"plain"`` forces the plain
    path (config equivalent of ``--cui``); ``"inline"`` selects the legacy
    bounded inline driver (retains upstream bugs #3285/#3286); ``"auto"`` and
    ``"alt-screen"`` both resolve to ``"alt-screen"`` (full-screen). An
    unrecognised mode is treated as the ``alt-screen`` default — config parsing
    already validates+warns, this is a belt-and-braces fallback.
    """
    if not is_tty:
        return "plain"
    if mode == "plain":
        return "plain"
    if mode == "inline":
        return "inline"
    # "alt-screen", "auto", or any unexpected value → full-screen alt-screen.
    return "alt-screen"


def _configured_render_mode(config) -> str:
    """Read ``config.chat.render_mode``; default ``alt-screen`` if unavailable."""
    default = "alt-screen"
    if config is None:
        return default
    try:
        return str(config.chat.render_mode)
    except AttributeError:
        return default


async def run_chat_client(
    *,
    transport: "ClientTransport",
    renderer: "ChatRenderer",
    read_model: "ChatReadModel",
    agent_name: str,
    is_tty: bool,
    config=None,
) -> None:
    """Input-driver selection + (plain) banner/output loop + wait + teardown.

    When the renderer ``uses_app_input()``, ``chat.render_mode`` (#3273) +
    ``is_tty`` are resolved by :func:`resolve_render_mode` to one of three paths:
    ``alt-screen`` / ``inline`` (both the Textual conversation-pane app
    :func:`~reyn.interfaces.inline.textual_chat.run_textual_chat`, which owns
    both input and output and drains ``transport.frames()`` itself — imported
    LAZILY here so the flowview/textual dependency is touched on that path only)
    or ``plain`` (the plain PromptSession input loop + shared output loop below).
    A non-TTY session (``--cui`` / piped / CI / no real terminal) always resolves
    to ``plain`` regardless of mode. This is the SAME selection ``run_repl`` made
    locally — now applied identically to the remote path.

    The caller owns transport lifecycle (``start``/``close`` or the httpx SSE
    context) and any cost summary; this function only drives the loop(s) and
    guarantees any tasks it spawns are cancelled + awaited on exit.
    """
    if renderer.uses_app_input():
        resolved = resolve_render_mode(
            _configured_render_mode(config), is_tty=is_tty,
        )
        if resolved != "plain":
            from reyn.interfaces.inline.textual_chat import run_textual_chat  # noqa: PLC0415
            await run_textual_chat(
                transport=transport,
                read_model=read_model,
                agent_name=agent_name,
                config=config,
                inline=(resolved == "inline"),
            )
            return

    renderer.banner(agent_name)

    # `set` = "no reply pending" (the PromptSession input loop's pacing gate is
    # open); `clear` = "a turn is in flight". The shared output loop always
    # signals it so the plain-path gate never hangs.
    reply_seen: asyncio.Event = asyncio.Event()
    reply_seen.set()

    # #3287: on an interactive TTY, `prompt_session.prompt_async` below already
    # leaves the typed line on the terminal the instant Enter is pressed — the
    # user-line echo already happened there. Without this queue the broadcast
    # `user_submitted` chat-event this same submission produces (see
    # `run_output_loop`) renders it a SECOND time, printing every LLM-round-trip
    # turn's own line twice (a local `/quit` never reaches `submit_user_text`,
    # so it never doubled — the exact contrast the bug report noted). `None` on
    # a non-TTY session leaves the event-driven render as the sole echo (there
    # is nothing else on screen to duplicate it against — piped stdin is never
    # echoed by the terminal). Owned by THIS client's own loop pair (never
    # shared), so it only ever matches submissions this same process made —
    # another attached client's turns still render normally (see both loops'
    # docstrings in `stream_client.py`).
    from collections import deque  # noqa: PLC0415
    own_submissions: "deque[str] | None" = deque() if is_tty else None

    from prompt_toolkit import PromptSession  # noqa: PLC0415
    from prompt_toolkit.history import FileHistory  # noqa: PLC0415
    from prompt_toolkit.styles import Style  # noqa: PLC0415
    prompt_session: "PromptSession[str]" = PromptSession(
        history=FileHistory(str(read_model.history_path)),
        style=Style.from_dict({"bottom-toolbar": "noreverse bg:default"}),
    )
    inputs = asyncio.create_task(
        run_input_loop(
            transport, prompt_session, renderer, reply_seen,
            own_submissions=own_submissions,
        )
    )

    outputs = asyncio.create_task(
        run_output_loop(
            transport, renderer, reply_seen,
            command_ui_region=read_model.has_command_ui_region,
            own_submissions=own_submissions,
        )
    )

    try:
        await asyncio.wait({inputs, outputs}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)


__all__ = ["resolve_render_mode", "run_chat_client"]
