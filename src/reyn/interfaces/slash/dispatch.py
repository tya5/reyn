"""The shared CLIENT-side slash layer — text in, a named command out (#3595 S5).

The owner's ruling for this arc is that a client interprets ``/``-prefixed text
and maps it onto published operations, and that ``Session`` never interprets a
string ("スラッシュコマンドの解釈は tui 側の想定だよ。inbox につまれたものは
スラッシュコマンドとして解釈されない。されるんだとするとそれが不具合" /
"cui / tui はスラッシュコマンド共通実装にすべき"). S1–S3 closed the inbox
vocabulary so a non-operator producer cannot claim ``CLIENT_INPUT``; S4 moved
what a handler is HANDED onto the client seam; this module is where the
interpretation itself now lives, shared by every client rather than duplicated
per client.

Two halves, on opposite sides of the transport:

- :func:`maybe_dispatch_slash` is the CLIENT half. It echoes the typed line,
  parses it, resolves it against the process-local ``REGISTRY`` (which every
  client has — it is imported code, not session state), and asks the transport
  to run the resolved command by NAME. Everything it displays on its own — the
  echo, the extra-lines note, the bare-``/`` catalog, the unknown-command
  suggestion — is client-authored display, so it goes through ``put_display``.
- :func:`execute_slash_command` is the EXECUTOR half, called wherever the
  session actually is: in-process for a local attach (``InProcessTransport``),
  server-side for a remote one (the AG-UI endpoint's ``slash_command`` arm). It
  never sees the operator's raw text — only a name that was already resolved
  against the registry, plus its argument string.

★ **What this changes about WHEN a slash command runs.** Before S5 an operator's
``/model …`` rode the inbox: it was queued behind an in-flight turn (#3300's
sent-queue) and dispatched only once that turn settled, because
``Session._handle_user_message`` was the thing that interpreted it. A client-side
layer has no inbox, so every slash command now runs immediately — the treatment
``/answer`` alone got from #3327's ``maybe_deliver_answer_command`` fast path,
generalized. That fast path is deleted rather than preserved: it existed because
a queued ``/answer`` chases its own precondition (the turn that would dequeue it
only frees when the intervention it answers resolves), and that argument was
never specific to ``/answer`` — it applies to any command meant to act on a
session that is currently busy. What is NOT widened is the sent-queue contract
for ordinary turns: bare text still goes to ``submit_user_text`` and still
queues, which is the invariant #3300 actually protects.

⚠️ Consequence worth naming rather than discovering: a slash handler now runs
CONCURRENTLY with an in-flight turn instead of after it. #3327 established that
shape for the answer funnel; S5 extends it to the whole catalog.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, AsyncIterator

from reyn.interfaces.slash import REGISTRY, SlashContext, suggest_for_unknown
from reyn.interfaces.transport.client_transport import ClientTransport

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import Frame
    from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)


def _display(transport: "ClientTransport", kind: str, text: str, **meta) -> None:
    from reyn.runtime.outbox import OutboxMessage
    transport.put_display(OutboxMessage(kind=kind, text=text, meta=dict(meta)))


async def maybe_dispatch_slash(
    transport: "ClientTransport", text: str, *, echo: bool = True,
) -> bool:
    """Interpret ``text`` as a slash command; ``True`` iff it was consumed.

    The one place a reyn client turns typed text into a command. A caller
    submits a line to it BEFORE ``submit_user_text``; a ``True`` return means
    the line was a command and must NOT also be submitted as a turn.

    Non-``/`` text is never touched (``False``, nothing displayed) — that is
    what keeps the ordinary-turn path, and the #3300 sent-queue behind it,
    exactly as it was.

    ★ **The typed line is echoed first.** A command's OUTPUT is client-authored
    display and rides ``put_display``; so is its INPUT, and
    ``ClientTransport.put_display``'s own docstring names "user echo" as its
    first payload. Showing one without the other is worse than showing neither:
    two runs of the same command produce two identical result blocks with
    nothing to attribute them to, and once a command can also run while a turn
    is in flight, a result cannot even be told from turn output. An ordinary
    turn's echo comes from the ``user_submitted`` audit-event (#3300 P1 C), which
    a command never emits — so this is the only surface that can produce it.

    ``echo=False`` is for a client whose own input surface ALREADY put the line
    on screen: the plain ``--cui`` driver on an interactive TTY, where
    ``prompt_session.prompt_async`` leaves the typed line in the terminal the
    instant Enter is pressed. Echoing there would re-print it — the #3287
    double-render, through a new door. Which side a client is on is a fact only
    that client knows; HOW to echo lives here, so there is still one
    implementation.

    Multi-line input: slash commands are line-oriented and take no multi-line
    args, so trailing lines are reported and dropped rather than silently
    bundled into ``args`` and ignored by whichever handler does not read them.
    The echo carries the WHOLE typed text, which is what makes the note about
    ignored lines legible.
    """
    if not text.startswith("/"):
        return False

    if echo:
        _display(transport, "user", text)

    first_line, sep, rest = text.partition("\n")
    if sep and rest.strip():
        _display(
            transport, "system",
            f"note: {first_line.split(maxsplit=1)[0]} ignored extra lines; "
            "only the first line is treated as the command.",
        )

    body = first_line[1:].lstrip()
    if not body:
        known = ", ".join(f"/{n}" for n in REGISTRY.names())
        _display(transport, "system", f"known commands: {known}")
        return True

    parts = body.split(maxsplit=1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    if REGISTRY.get(name) is None:
        # Suggest the 3 closest matches rather than dumping the full catalog:
        # the full list used to truncate mid-name, hiding the actionable
        # suggestions. ``kind="error"`` so the TUI renders an inline error — a
        # ``system`` line is indistinguishable from a successful reply, which
        # made a typo'd command silently look OK.
        known = ", ".join(f"/{n}" for n in suggest_for_unknown(name))
        _display(transport, "error", f"unknown command /{name}; try: {known}")
        return True

    ran = await transport.run_slash_command(name, args)
    if not ran:
        _display(
            transport, "error",
            f"/{name} could not run: this client has no session to run it on.",
        )
    return True


class _ErrorWatchingTransport(ClientTransport):
    """A pass-through client transport that remembers whether an error was shown.

    The recall hint below needs one bit — "did this command report a failure" —
    and the only truthful source is what the handler actually displayed. Before
    S5 the session read that off ``outbox._queue``, asyncio's PRIVATE deque,
    inside a ``try/except`` that existed because a CPython internals change would
    otherwise break slash dispatch rather than just the hint. Wrapping the seam
    the handler already writes through answers the same question with no
    internals access at all.

    Delegation is total and explicit: every method forwards, so a handler that
    reaches for any other part of the seam reaches the real one.
    """

    def __init__(self, inner: "ClientTransport") -> None:
        self._inner = inner
        self.saw_error = False

    def put_display(self, msg: "OutboxMessage") -> None:
        if msg.kind == "error":
            self.saw_error = True
        self._inner.put_display(msg)

    def start(self) -> None:
        self._inner.start()

    async def state_ready(self) -> None:
        # #5050 ③ follow-up (CI-caught, test_error_watching_transport_
        # total_delegation_4884.py): without this override, this wrapper
        # falls back to ``ClientTransport``'s own base default (return
        # immediately) instead of the WRAPPED transport's real readiness
        # — a slash handler asking "has state landed" would get told
        # "yes" instantly regardless of the inner transport's actual
        # state, the same lying-ready shape architect's switch-case
        # finding named, just a second delegation site it rides through.
        await self._inner.state_ready()

    def close(self) -> None:
        self._inner.close()

    def frames(self) -> "AsyncIterator[Frame]":
        return self._inner.frames()

    def has_session(self) -> bool:
        return self._inner.has_session()

    def attach_failed(self) -> bool:
        return self._inner.attach_failed()

    def pending_intervention_head(self) -> "object | None":
        return self._inner.pending_intervention_head()

    async def clear_pending_command_ui(self) -> None:
        await self._inner.clear_pending_command_ui()

    def reyn_state_root(self) -> "Path | None":
        return self._inner.reyn_state_root()

    async def submit_user_text(self, text: str) -> str:
        return await self._inner.submit_user_text(text)

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return await self._inner.answer_intervention_text(
            text, intervention_id=intervention_id
        )

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return await self._inner.answer_intervention_choice(
            choice_id, intervention_id=intervention_id
        )

    async def cancel_inflight(self) -> str:
        return await self._inner.cancel_inflight()

    async def cancel_queued(self, msg_id: str) -> bool:
        return await self._inner.cancel_queued(msg_id)

    async def request_attach(self, agent_name: str) -> bool:
        return await self._inner.request_attach(agent_name)

    async def request_session_switch(self, session_id: str) -> bool:
        return await self._inner.request_session_switch(session_id)

    async def request_artifact_list(
        self, *, agent: str
    ) -> "tuple[list[dict], int]":
        return await self._inner.request_artifact_list(agent=agent)

    async def run_slash_command(self, name: str, args: str) -> bool:
        return await self._inner.run_slash_command(name, args)

    async def shutdown(self) -> None:
        await self._inner.shutdown()


async def execute_slash_command(ctx: SlashContext, name: str, args: str) -> bool:
    """Run the registered command ``name`` against ``ctx``; ``True`` iff it ran.

    The executor half. It takes a NAME, never the operator's raw text: the
    interpretation happened client-side in :func:`maybe_dispatch_slash`, so
    nothing on this side of the transport sniffs a string to decide what to do.
    An unknown name still returns ``False`` rather than raising — a client and a
    server can be running different builds, and a stale name must not read as a
    crash.

    A raising handler is contained: ``Session.run()``'s
    ``while await run_one_iteration()`` has no ``except``, so an uncaught error
    from a handler used to end the session run loop and silently drop every later
    inbox message (the front-end kept accepting input but never replied).
    ``CancelledError`` is a ``BaseException`` and is deliberately not caught, so
    shutdown still cancels.
    """
    cmd = REGISTRY.get(name)
    if cmd is None:
        return False
    watch = _ErrorWatchingTransport(ctx.transport)
    watched = replace(ctx, transport=watch)
    try:
        await cmd.handler(watched, args)
    except Exception as e:  # noqa: BLE001 — a handler must never kill the loop
        logger.exception("slash handler /%s failed", name)
        detail = f"{type(e).__name__}: {e}"
        if len(detail) > 72:
            detail = detail[:69] + "…"
        _display(ctx.transport, "error", f"/{name} failed: {detail}")
        return True
    if watch.saw_error:
        _display(
            ctx.transport, "status", f"↑ to recall `/{name}`",
            source="slash_recall_hint",
        )
    return True


__all__ = ["maybe_dispatch_slash", "execute_slash_command"]
