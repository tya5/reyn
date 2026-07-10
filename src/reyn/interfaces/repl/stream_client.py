"""The stream-consuming chat client (ADR-0039 P1).

This module is the CLIENT half of the transport seam: it consumes a
:class:`~reyn.interfaces.transport.client_transport.ClientTransport`'s unified
frame stream and drives the renderer, and it routes user input back through the
transport's send side. It is the plain / ``--cui`` (PromptSession) driver and
the shared output loop for the interactive inline driver.

The defining property is **single-writer by construction**: this module touches
the world ONLY through the ``ClientTransport`` it is handed — it imports no
``Session`` / ``Workspace`` / tool / registry surface (enforced by
``tests/test_stream_client_single_writer_boundary.py``). One stream comes in;
the renderer's two entry points (``message`` for display frames,
``on_chat_event`` for event frames) go out, dispatched by frame tag. This is
what makes the future remote client (P2) single-writer-safe for free: it is the
same client, a different transport.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.patch_stdout import patch_stdout

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import FrameTag
from reyn.runtime.outbox import OutboxMessage

from ._clipboard import copy_to_clipboard_async
from .renderer import ChatRenderer

logger = logging.getLogger(__name__)

# How many recent agent replies `/copy` can target (1 = newest).
_COPY_BUFFER_MAX = 20


def _copy_target(recent_replies, arg: str) -> tuple[str | None, str]:
    """Pure: resolve a ``/copy`` arg against the newest-first reply buffer.

    Returns ``(text_to_copy, status)``. ``text_to_copy`` is None when there is
    nothing to copy — ``status`` then explains why (list view / empty buffer /
    bad arg / out of range). ``recent_replies[0]`` is the newest reply.
    """
    arg = (arg or "").strip()
    n_buf = len(recent_replies)

    def _plural(n: int) -> str:
        return "reply" if n == 1 else "replies"

    if arg == "list":
        if not n_buf:
            return None, "no replies buffered yet"
        return None, f"{n_buf} {_plural(n_buf)} buffered (/copy N — 1 = newest)"
    n = 1
    if arg:
        if not arg.isdigit() or int(arg) < 1:
            return None, f"bad /copy arg {arg!r}; use a number (1 = newest) or 'list'"
        n = int(arg)
    if not n_buf:
        return None, "no agent reply to copy yet"
    if n > n_buf:
        return None, f"only {n_buf} {_plural(n_buf)} buffered"
    return recent_replies[n - 1], ""


async def _handle_copy_sentinel(recent_replies, arg: str):
    """Resolve a ``/copy`` request and return a status OutboxMessage to render.

    Replaces the unhandled ``__copy_last_reply__`` sentinel (a silent no-op
    before this) with a real clipboard copy + a visible result line.
    """
    text, status = _copy_target(recent_replies, arg)
    if text is not None:
        ok, tool = await copy_to_clipboard_async(text)
        status = (
            f"copied reply to clipboard ({tool})" if ok
            else "no clipboard tool found — install pbcopy / xclip / wl-copy / xsel"
        )
    return _simple_status(status)


def _simple_status(text: str) -> OutboxMessage:
    """Build a status OutboxMessage for inline rendering (no async needed)."""
    return OutboxMessage(kind="status", text=text)


async def run_input_loop(
    transport: ClientTransport,
    prompt_session: PromptSession,
    renderer: ChatRenderer,
    reply_seen: "asyncio.Event | None" = None,
) -> None:
    is_tty = sys.stdin.isatty()
    while True:
        # Piped / scripted mode: pace input by reply availability. Without
        # this gate, readline pulls every buffered line before the output
        # loop renders the first reply — the per-turn `reply_seen.clear()`
        # races with `set()`, and any later `wait_for(reply_seen)` may be
        # satisfied by an earlier turn's reply instead of the current one.
        # The gate serialises turns: read line N+1 only after turn N's
        # reply has been rendered (or there is no pending turn at all).
        # TTY mode is unaffected — interactive users may type ahead.
        if not is_tty and reply_seen is not None:
            await reply_seen.wait()

        try:
            if is_tty:
                with patch_stdout():
                    text = await prompt_session.prompt_async(
                        renderer.prompt_text(),
                        # Animated working indicator while a turn runs. None when
                        # idle (default base renderer) → no toolbar shown.
                        bottom_toolbar=renderer.bottom_toolbar,
                        refresh_interval=0.1,
                        # #2786: prompt_toolkit's default (True) swaps the
                        # loop's asyncio exception handler for its own for the
                        # duration of this call (application.py's
                        # set_exception_handler_ctx contextmanager) -- and the
                        # prompt-wait is most of the REPL's wall-clock time, so
                        # that window masks #2637's durable
                        # install_asyncio_exception_handler capture almost
                        # permanently. False leaves reyn's handler wired
                        # (prompt_toolkit's own KeyboardInterrupt/EOF handling
                        # lives in the key-binding layer, not this handler, so
                        # nothing else regresses -- see asyncio_diagnostics.py).
                        set_exception_handler=False,
                    )
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
            # The pacing gate above guarantees any in-flight reply has
            # already been rendered before we read the next line, so we
            # can shut down immediately without a drain timeout.
            await transport.shutdown()
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            await transport.shutdown()
            return
        if not transport.has_session():
            renderer.message(_simple_status("no agent attached; try :agents"))
            continue
        await route_input_line(transport, text, reply_seen)


async def route_input_line(
    transport: ClientTransport, text: str, reply_seen: "asyncio.Event | None"
) -> None:
    """Route one non-quit client line to the session via the transport.

    A pending intervention (permission prompt, ask_user, safety-limit) suspends
    the router turn on the intervention's future — and that turn is the SOLE
    consumer of the session inbox, so an answer routed the ordinary way
    (``submit_user_text`` → inbox) can never be dequeued while the turn is
    blocked: the future is never resolved and the session hangs indefinitely
    (#2690 — the file-write approval prompt "never resumes after answering y").
    A non-slash line is therefore delivered DIRECTLY to the pending intervention
    via the transport's ``answer_intervention_text`` seam (which wraps the same
    session ``answer_oldest_intervention_text`` the inline CUI's concurrent
    region poll uses), bypassing the inbox so the future resolves and the
    blocked turn resumes.

    Ordinary turns (no pending intervention) still flow through
    ``submit_user_text``; a ``/``-prefixed line is never an intervention answer,
    so it is left on the normal slash/inbox path. The direct-delivery result is
    checked so a race where the intervention resolves between the head-check and
    the deliver falls back to a normal turn instead of being dropped.
    """
    if not text.startswith("/") and transport.pending_intervention_head() is not None:
        if await transport.answer_intervention_text(text):
            return
    # Mark a reply as in flight before submit so the pacing gate on the next
    # iteration blocks until the output loop signals it. `/`-prefixed slash
    # commands (`/list`, `/attach`, `/answer`, ...) bypass the router and emit
    # only `status` (no router reply), so clearing the gate for them would
    # deadlock the next pipe iteration. `/quit` and `/exit` are handled by the
    # caller and never reach here.
    if reply_seen is not None and not text.startswith("/"):
        reply_seen.clear()
    await transport.submit_user_text(text)


async def run_output_loop(
    transport: ClientTransport,
    renderer: ChatRenderer,
    reply_seen: "asyncio.Event | None" = None,
) -> None:
    is_tty = sys.stdout.isatty()
    # Newest-first ring of recent agent replies so `/copy [N]` can grab the
    # latest (or an older) reply and pipe it to the system clipboard.
    recent_replies: deque[str] = deque(maxlen=_COPY_BUFFER_MAX)
    async for frame in transport.frames():
        # Event frame → the renderer's working-indicator entry point. The dual
        # stream is dispatched by tag at the CONSUMING end so the renderer keeps
        # its two entry points; an outbox-only stream would silently drop these
        # (the A2 WaitingOn bug, designed out by the completeness gate).
        if frame.tag is FrameTag.EVENT:
            renderer.on_chat_event(frame.event)
            continue
        msg = frame.message
        if msg.kind == "__end__":
            return
        if msg.kind == "agent":
            recent_replies.appendleft(msg.text)
        elif msg.kind == "__copy_last_reply__":
            # /copy sentinel: resolve + copy, then render the result as a status
            # line instead of the (unhandled) sentinel — no more silent no-op.
            msg = await _handle_copy_sentinel(recent_replies, msg.text)
        elif msg.kind == "__rewind_list__":
            # /rewind picker (F4): the inline path shows a ↑↓ region selector
            # (driven by session.pending_command_ui), so skip the text list there;
            # the plain --cui path renders it as the fallback.
            if renderer.uses_app_input():
                continue
            # persistent kind (not transient "status") so the list stays readable
            msg = OutboxMessage(kind="intervention", text=msg.text)
        # On a real terminal: wrap in run_in_terminal so the prompt is cleared
        # before output and redrawn after — required for ANSI/Rich to render
        # cleanly without corrupting the prompt.
        # On a pipe: print plainly, no prompt redraw, no cursor escapes.
        #
        # Contain a single message's render failure: this loop is the sole
        # consumer of the transport frame stream, so an uncaught exception here
        # would end the loop, trip run_repl's FIRST_COMPLETED wait, and tear
        # down the whole REPL for one bad message. Log and continue instead
        # (CancelledError is BaseException, so shutdown cancellation still
        # propagates). reply_seen is still signalled below so the input pacing
        # gate never hangs.
        try:
            if is_tty and get_app_or_none() is not None:
                await run_in_terminal(lambda m=msg: renderer.message(m))
            else:
                renderer.message(msg)
        except Exception:
            logger.exception("output loop: render failed for message kind=%r", msg.kind)
        # Signal end-of-turn for the input loop's pacing gate. "agent" is
        # the canonical reply kind; "error" also counts as turn-terminal so
        # a failed router round doesn't deadlock the next iteration.
        if reply_seen is not None and msg.kind in {"agent", "error"}:
            reply_seen.set()


__all__ = [
    "route_input_line",
    "run_input_loop",
    "run_output_loop",
]
