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

    Returns one of ``"alt-screen"``, ``"plain"`` — the two physical render
    paths (the first is the Textual conversation-pane app's one driver mode;
    the second is the plain :class:`ChatRenderer`). #4223 removed the legacy
    ``"inline"`` bounded driver and the ``"auto"`` config value (owner
    instruction — the latter was behaviourally IDENTICAL to ``"alt-screen"``
    given the TTY guard below, a name-only third option).

    The TTY guard is universal: a non-TTY session (piped, CI, sandbox with no
    real terminal, or a host where alt-screen would silently no-op) can never
    enter an interactive Textual driver, so it always resolves to ``"plain"``
    regardless of the configured mode. On a TTY: ``"plain"`` resolves to the
    plain path here too, but (#3292) that is now the SECOND of two gates —
    ``chat.py``'s renderer selection already forces ``ConsoleChatRenderer``
    whenever ``chat.render_mode`` is ``"plain"``, so ``renderer.uses_app_input()``
    is already ``False`` and this function is never reached with an app-input
    renderer on a standard ``reyn chat`` invocation; this branch is a
    defensive fallback for any caller that reaches here with an app-input
    renderer despite a ``"plain"`` config. The net effect (renderer forced
    Console + this fallback) is genuine ``--cui`` equivalence, not a hybrid.
    Any unrecognised mode (including a stale ``"inline"``/``"auto"`` left
    over in an operator's config after #4223) is treated as the
    ``"alt-screen"`` default — config parsing already validates+warns, this
    is a belt-and-braces fallback.
    """
    if not is_tty:
        return "plain"
    if mode == "plain":
        return "plain"
    # #4223: "inline" removed. "alt-screen" or any unexpected value (a stale
    # "inline"/"auto" left over in an operator's config, or anything else)
    # → full-screen alt-screen — the SAME fall-through this line already
    # provided for "auto"/unexpected before #4223, deliberately kept wide
    # rather than narrowed to `if mode == "alt-screen":` (architect's
    # invariant, #4223: narrowing this line removes the belt half of the
    # belt-and-braces pairing with `_build_render_mode`'s own warn-fallback
    # — an unexpected value that slips past config validation would then
    # have nowhere to land).
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
    own_connection_id: "str | None" = None,
) -> None:
    """Input-driver selection + (plain) banner/output loop + wait + teardown.

    When the renderer ``uses_app_input()``, ``chat.render_mode`` (#3273,
    narrowed to 2 values by #4223) + ``is_tty`` are resolved by
    :func:`resolve_render_mode` to one of two paths: ``alt-screen`` (the
    Textual conversation-pane app
    :func:`~reyn.interfaces.inline.textual_chat.run_textual_chat`, which owns
    both input and output and drains ``transport.frames()`` itself — imported
    LAZILY here so the flowview/textual dependency is touched on that path only)
    or ``plain`` (the plain PromptSession input loop + shared output loop below).
    A non-TTY session (``--cui`` / piped / CI / no real terminal) always resolves
    to ``plain`` regardless of mode. This is the SAME selection ``run_repl`` made
    locally — now applied identically to the remote path.

    (#3292, local path only: the local ``reyn chat`` call site already forces
    ``renderer`` to ``ConsoleChatRenderer`` — ``uses_app_input() is False`` —
    whenever ``chat.render_mode`` is ``"plain"`` on a TTY, so this function's own
    ``"plain"`` branch inside :func:`resolve_render_mode` is a defensive
    fallback there, not the live path. The remote call site does not yet wire
    ``chat.render_mode`` into its own renderer selection, so this function's
    ``"plain"`` branch is still load-bearing for a remote invocation.)

    ``own_connection_id`` (#3287/#3309 F2): the REMOTE call site's own
    ``connection_id`` (``remote_client.py`` mints it client-side, before any
    submit, and sends it on every POST) — ``None`` for the local path (no such
    concept; ``InProcessTransport`` correlates the own-echo suppression by
    ``msg_id`` instead, see below). Passed through to :func:`run_output_loop`
    only when ``is_tty`` (nothing else has shown the line otherwise, so the
    event-driven render must stay the sole record).

    The caller owns transport lifecycle (``start``/``close`` or the httpx SSE
    context) and any cost summary; this function only drives the loop(s) and
    guarantees any tasks it spawns are cancelled + awaited on exit.
    """
    if renderer.uses_app_input():
        resolved = resolve_render_mode(
            _configured_render_mode(config), is_tty=is_tty,
        )
        if resolved != "plain":
            from reyn.runtime.startup_timing import (  # noqa: PLC0415
                mark_tui_import_done,
                stage,
            )

            # #3671 client-prep breakdown (architect's design, P3): this is
            # the FIRST touch of textual/textual_flowview on this path — the
            # import is lazy so that cost is paid here, not in the `import`
            # stage (which closes at `mark_cli_reached`, before this module
            # is even imported). Prime suspect for the 19.46s `client-prep`
            # owner measured: owner's own `import 4.60s` is reyn's import
            # tree only, and does not include this.
            with stage("client-prep:tui-import"):
                from reyn.interfaces.inline.textual_chat import run_textual_chat  # noqa: PLC0415
            mark_tui_import_done()
            # #4223: `resolve_render_mode` can no longer return "inline" (the
            # config value was removed), so `resolved` here is always
            # "alt-screen" — no `inline=` kwarg to compute; `run_textual_chat`'s
            # own `inline: bool = False` default already selects alt-screen
            # (that parameter and its behavior are untouched, #4223's
            # invariant — see the function's own docstring).
            await run_textual_chat(
                transport=transport,
                read_model=read_model,
                agent_name=agent_name,
                config=config,
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
    # user-line echo already happened there. Without suppressing it, the
    # broadcast `user_submitted` audit-event this same submission produces (see
    # `run_output_loop`) renders it a SECOND time, printing every LLM-round-trip
    # turn's own line twice (a local `/quit` never reaches `submit_user_text`,
    # so it never doubled — the exact contrast the bug report noted). `None`
    # (own_submissions AND own_connection_id) on a non-TTY session leaves the
    # event-driven render as the sole echo (there is nothing else on screen to
    # duplicate it against — piped stdin is never echoed by the terminal).
    #
    # TWO correlation mechanisms, one per transport shape, never text (a
    # same-text collision between two attached clients both typing "yes" would
    # misfire — co-vet finding on #3309, F1):
    #   - `own_connection_id` (F2): when the caller passes one (the REMOTE
    #     path — `remote_client.py` mints its own `connection_id` client-side,
    #     BEFORE any submit, and stamps it on every POST; the server echoes it
    #     straight back into the broadcast event's `meta.auth_connection_id`,
    #     #3300's existing attribution plumbing) `run_output_loop` matches on
    #     that field directly — structurally race-free because the id is known
    #     up-front, with no dependency on the POST-ack/SSE-broadcast arrival
    #     order at all (closing the residual race an earlier revision of this
    #     fix could only document, not eliminate).
    #   - `own_submissions` (msg_id set, #3287/F1): the LOCAL path
    #     (`InProcessTransport`) has no connection-id concept, so it keeps the
    #     msg_id-based set instead — also race-free (same-task, no yield point
    #     between the audit-event emit and the id reaching the caller).
    # Only one is ever active per session (the other stays `None`) — never
    # shared across clients either way, so another attached client's turns
    # always render normally (see both loops' docstrings in `stream_client.py`).
    own_connection_id = own_connection_id if is_tty else None
    own_submissions: "set[str] | None" = (
        set() if (is_tty and own_connection_id is None) else None
    )

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
            own_connection_id=own_connection_id,
        )
    )

    try:
        await asyncio.wait({inputs, outputs}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)


__all__ = ["resolve_render_mode", "run_chat_client"]
