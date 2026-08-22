"""The stream-consuming chat client (ADR-0039 P1).

This module is the CLIENT half of the transport seam: it consumes a
:class:`~reyn.interfaces.transport.client_transport.ClientTransport`'s unified
frame stream and drives the renderer, and it routes user input back through the
transport's send side. It is the plain / ``--cui`` (PromptSession) driver and
the shared output loop for the interactive inline driver.

The defining property is **single-writer by construction**: this module touches
the world ONLY through the ``ClientTransport`` it is handed — it imports no
``Session`` / ``Workspace`` / tool / registry surface (enforced by
``tests/repo/test_stream_client_single_writer_boundary.py``). One stream comes in;
the renderer's two entry points (``message`` for display frames,
``on_audit_event`` for event frames) go out, dispatched by frame tag. This is
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

from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import FrameTag
from reyn.runtime.outbox import OutboxMessage

from ._copy_sentinel import COPY_BUFFER_MAX, handle_copy_sentinel
from .renderer import ChatRenderer

logger = logging.getLogger(__name__)


def _simple_status(text: str) -> OutboxMessage:
    """Build a status OutboxMessage for inline rendering (no async needed)."""
    return OutboxMessage(kind="status", text=text)


def _pending_head_id(head: object) -> "str | None":
    """Extract the id from :meth:`ClientTransport.pending_intervention_head`'s
    two established return shapes (#5057): a real ``UserIntervention``
    (``InProcessTransport``/``SessionBoundTransport`` — #5047 axis A
    guarantees its ``.id`` is a genuine identity, not merely "the oldest"),
    or a bare id string (``AgUiTransport``'s own ``_pending_intervention_id``).

    An unrecognized third shape returns ``None`` (logged) rather than
    silently coercing it into SOME string via ``str(...)`` — architect's own
    review finding on this PR (#5057): a naive ``getattr(head, "id", head)``
    would happily turn e.g. a dict-shaped value (a genuinely similar-looking
    but DIFFERENT contract lives nearby, ``RemoteReadModel.intervention_
    head()``'s own dict projection) into a garbage id string via ``str()``
    — the exact #4996-family "failure that looks like success" this repo's
    own vocabulary names. Never reached by the two production transports
    today (this IS a strict narrowing of a check that never fires yet, not
    a behavior change for either), but the id this function returns feeds
    straight into ``answer_intervention_by_id`` — a caller passing a
    genuinely wrong shape deserves "no pending intervention recognized"
    (falls through to a normal turn), never a corrupted delivery target."""
    if isinstance(head, str):
        return head
    iv_id = getattr(head, "id", None)
    if isinstance(iv_id, str) and iv_id:
        return iv_id
    if head is not None:
        logger.warning(
            "#5057: pending_intervention_head() returned an unrecognized "
            "shape (%s) -- neither a bare id string nor an object with a "
            "genuine string .id. Treating as no pending intervention "
            "rather than deriving a garbage id from it.",
            type(head).__name__,
        )
    return None


async def run_input_loop(
    transport: ClientTransport,
    prompt_session: PromptSession,
    renderer: ChatRenderer,
    reply_seen: "asyncio.Event | None" = None,
    *,
    own_submissions: "set[str] | None" = None,
) -> None:
    """Drive the plain PromptSession input loop.

    ``own_submissions`` (#3287): on an interactive TTY, ``prompt_session.
    prompt_async`` itself leaves the typed line ("you > <text>") on the
    terminal once Enter is pressed — that IS the user-line echo. Every
    submitted line is handed to :func:`route_input_line`, which records the
    ``msg_id`` the transport's ``submit_user_text`` assigns (the SAME
    correlation id — #3300 P2a — the broadcast ``user_submitted`` audit-event
    carries) into this set, so :func:`run_output_loop` can recognise that
    event BY ID and skip re-rendering it — otherwise the terminal's own echo
    and the event-driven echo both land, printing the line twice. Correlating
    by id rather than by text avoids a same-text collision misfire (e.g. two
    attached clients both typing "yes") that a text match cannot distinguish.
    Callers pass ``None`` (the non-interactive / piped path, where the
    terminal shows nothing as input streams in — the event-driven render is
    then the ONLY record of what was submitted, so it must not be suppressed)
    or a fresh ``set()`` owned by this client's own loop pair (never shared
    across clients, so another attached client's submissions are never
    matched here and always render — see ``run_output_loop``'s docstring).
    """
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
        await route_input_line(
            transport, text, reply_seen,
            own_submissions=own_submissions,
            terminal_echoed=is_tty,
        )


async def route_input_line(
    transport: ClientTransport,
    text: str,
    reply_seen: "asyncio.Event | None",
    *,
    own_submissions: "set[str] | None" = None,
    terminal_echoed: bool = False,
) -> None:
    """Route one non-quit client line to the session via the transport.

    #3595 S5: a ``/``-prefixed line is a COMMAND, and this client interprets it
    itself — through the shared client-side layer both reyn clients call, never
    by handing the string to the session. It is consumed here and never reaches
    ``submit_user_text``, so the branches below see turn text only.

    ``terminal_echoed`` answers the one question about a command line that only
    this client can: whether its own input surface already put the line on
    screen. On an interactive TTY ``prompt_session.prompt_async`` did (the same
    fact ``own_submissions`` exists for, #3287), so the shared layer must not
    echo it again; on a piped run nothing did, and the echo is the only record
    of what was asked for. A command emits no ``user_submitted`` audit-event, so
    the suppression ``own_submissions`` performs for a TURN has no equivalent
    here — the decision has to be made before the display is written, not after.

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
    ``submit_user_text``. The direct-delivery result is checked so a race where
    the intervention resolves between the head-check and the deliver falls back
    to a normal turn instead of being dropped.

    ``own_submissions`` (#3287) is populated ONLY on the branch that actually
    calls ``submit_user_text`` — that is the ONLY branch that produces a
    ``user_submitted`` audit-event for ``run_output_loop`` to match against. An
    intervention answer takes the direct-delivery branch above and returns
    before reaching it, so nothing is added for it (there is no matching
    ``user_submitted`` event to ever consume such an entry). The transport's
    ``submit_user_text`` return value is the server-assigned ``msg_id`` — the
    SAME id the broadcast event carries — added to the set verbatim; an empty
    string (no session attached / a non-conforming transport) is never added,
    so it can never accidentally match an event whose own ``msg_id`` also
    reads empty/missing.
    """
    if await maybe_dispatch_slash(transport, text, echo=not terminal_echoed):
        return
    # Everything below is TURN text: a command returned above, so the two
    # ``not text.startswith("/")`` guards this function used to carry are gone
    # rather than left as conditions that can no longer be false (#3595 S5).
    head = transport.pending_intervention_head()
    if head is not None:
        # #5057 (architect's own trace): deliver BY the id CAPTURED HERE, at
        # check time — never let the transport funnel re-read "whichever
        # intervention is head NOW" at delivery time. Before this, this
        # branch called `answer_intervention_text(text)` with NO id at all,
        # which fell through to `answer_oldest_intervention_text` — the
        # SAME head-of-queue race #3299 P2's own `answer_intervention_by_id`
        # (R1) was invented to close for every OTHER surface: if a second
        # queued intervention resolves (via `/answer`, an A2A peer, …)
        # between this check and the reply being typed, the NEXT head is a
        # DIFFERENT intervention than the one the operator is answering —
        # the exact multi-pending misdelivery #5047 traced. `head` here is
        # ALWAYS a genuine `UserIntervention` (in-process/session-bound) or
        # a bare id string (the AG-UI remote transport, `client.py`'s own
        # `_pending_intervention_id`) — `getattr(head, "id", head)`
        # extracts the id either way, not sniffed on ambiguity, just the
        # two established production shapes (#5047 axis A guarantees
        # `head.id` is a real identity now, not merely "the oldest").
        iv_id = _pending_head_id(head)
        if await transport.answer_intervention_text(text, intervention_id=iv_id):
            return
    # Mark a reply as in flight before submit so the pacing gate on the next
    # iteration blocks until the output loop signals it. A command would
    # deadlock the next pipe iteration here — it emits only `status`, never a
    # router reply — which is why it must have been consumed above; `/quit` and
    # `/exit` are handled by the caller and never reach here at all.
    if reply_seen is not None:
        reply_seen.clear()
    msg_id = await transport.submit_user_text(text)
    if own_submissions is not None and msg_id:
        own_submissions.add(msg_id)


async def run_output_loop(
    transport: ClientTransport,
    renderer: ChatRenderer,
    reply_seen: "asyncio.Event | None" = None,
    *,
    command_ui_region: bool = True,
    own_submissions: "set[str] | None" = None,
    own_connection_id: "str | None" = None,
) -> None:
    """Drain the transport's unified frame stream to the renderer.

    A ``user_submitted`` audit-event is a BROADCAST — every attached client
    (this one included) gets it, so a second attached client can still be
    relying on it as the only render of a turn it didn't type itself. On an
    interactive TTY, THIS client's own submissions are different:
    ``prompt_session.prompt_async`` already left the typed line on the
    terminal the instant Enter was pressed, so re-rendering it from the
    broadcast event would print it twice (#3287). Two DIFFERENT correlation
    mechanisms recognise "this is the event for the line my OWN input loop
    just showed" — never by TEXT, so two attached clients submitting the SAME
    text (e.g. both typing "yes") never cross-match: a text match would let
    client A's broadcast satisfy client A's queue check for client B's
    identical-text turn (or vice-versa, depending on arrival order),
    simultaneously swallowing the OTHER client's line and leaving THIS
    client's own line unswallowed later (the original bug, reintroduced by a
    different route — co-vet finding F1 on #3309; see
    ``tests/interfaces/test_stream_client_own_echo_3287.py``
    ``test_same_text_two_clients_does_not_cross_match_by_id``):

    - ``own_connection_id`` (F2, checked FIRST): the caller's own
      ``connection_id`` — for the AG-UI remote path, minted client-side
      (``remote_client.py``) BEFORE any submit and stamped on every POST; the
      server echoes it straight back into the broadcast event's
      ``meta.auth_connection_id`` (the SAME attribution field #3300 already
      wired for multi-client display). Matched directly against the event's
      ``meta.auth_connection_id`` — structurally race-free, since the id is
      known up-front and this check has NO dependency on any OTHER channel
      (in particular, none on the POST-ack that returns ``submit_user_text``'s
      ``msg_id`` racing the SSE broadcast — the residual gap an earlier
      revision of this fix could only document, not eliminate).
    - ``own_submissions`` (a ``msg_id`` set, F1): the LOCAL path
      (``InProcessTransport``) has no connection-id concept, so it correlates
      by the ``msg_id`` its ``submit_user_text`` call returns instead — also
      race-free (same-task, no yield point between the audit-event emit and
      the id reaching the caller).

    Only one of the two is ever non-``None`` for a given session (see
    ``client_driver.run_chat_client``). Events for anyone else's submissions
    still render normally either way — the fix is about which surface owns
    the echo (the terminal, for this client's own TTY-typed line) not about
    dropping the event universally. Both ``None`` (the default, and what
    non-interactive / piped / non-TTY sessions pass) disables the check
    entirely, preserving the event-driven echo as the sole record when
    nothing else showed the line.
    """
    is_tty = sys.stdout.isatty()
    # Newest-first ring of recent agent replies so `/copy [N]` can grab the
    # latest (or an older) reply and pipe it to the system clipboard.
    recent_replies: deque[str] = deque(maxlen=COPY_BUFFER_MAX)
    async for frame in transport.frames():
        # Event frame → the renderer's working-indicator entry point. The dual
        # stream is dispatched by tag at the CONSUMING end so the renderer keeps
        # its two entry points; an outbox-only stream would silently drop these
        # (the A2 WaitingOn bug, designed out by the completeness gate).
        if frame.tag is FrameTag.EVENT:
            event = frame.event
            if getattr(event, "type", None) == "user_submitted":
                data = getattr(event, "data", None) or {}
                if own_connection_id is not None:
                    meta = data.get("meta") or {}
                    if meta.get("auth_connection_id") == own_connection_id:
                        # This client's own remote submission (matched BY
                        # CONNECTION IDENTITY, #3309 F2) — its own POST already
                        # produced the terminal echo, no wait on any other
                        # channel needed. Skip the redundant re-render.
                        continue
                elif own_submissions:
                    event_msg_id = data.get("msg_id")
                    if event_msg_id and event_msg_id in own_submissions:
                        # This client's own PromptSession prompt already
                        # echoed this exact submission (matched BY ID, #3287)
                        # to the terminal — skip the redundant re-render and
                        # consume the matched id so the set can't accidentally
                        # match a LATER, unrelated event (ids never reused).
                        own_submissions.discard(event_msg_id)
                        continue
            renderer.on_audit_event(event)
            continue
        msg = frame.message
        if msg.kind == "__end__":
            return
        if msg.kind == "agent":
            recent_replies.appendleft(msg.text)
        elif msg.kind == "__copy_last_reply__":
            # /copy sentinel: resolve + copy, then render the result as a status
            # line instead of the (unhandled) sentinel — no more silent no-op.
            msg = await handle_copy_sentinel(recent_replies, msg.text)
        elif msg.kind == "__rewind_list__":
            # /rewind picker (F4): the LOCAL inline path shows a ↑↓ region selector
            # (driven by session.pending_command_ui), so skip the text list there;
            # the plain --cui path renders it as the fallback. command_ui_region is
            # False for a REMOTE inline client (ADR-0039 P3): command-UI is not on
            # the AG-UI wire, so with the inline renderer selected remotely we must
            # STILL take the text fallback — otherwise remote /rewind would be
            # swallowed for a picker that never arrives.
            if renderer.uses_app_input() and command_ui_region:
                continue
            # persistent kind (not transient "status") so the list stays
            # readable. #5047 (axis A): was "intervention" purely for this
            # persistence property, not because a rewind list IS a
            # question — OutboxMessage.__post_init__ now requires a genuine
            # meta["intervention_id"] for that family, which this frame
            # never had. "system" is the SAME "persistent info row" kind
            # OutboxMessage.from_wire demotes an identity-less wire
            # intervention frame to — giving rewind its OWN distinct kind
            # (axis C) is a separate, later step; this keeps the existing
            # persistent-render behavior without claiming an identity this
            # frame never had.
            msg = OutboxMessage(kind="system", text=msg.text)
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
