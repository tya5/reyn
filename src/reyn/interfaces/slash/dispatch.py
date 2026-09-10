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

★ **#5837 stage 2 — ``!``/``!!`` are a CLIENT-side spelling, not a second
dispatch route.** Owner ruling (verbatim): "'!'/'!!' 解釈は core ではなく '/'
と同様 remote 側責務期待" — the same client-owns-interpretation shape this
module already gives ``/``. :func:`maybe_dispatch_slash` normalizes a
``!``/``!!``-prefixed line into the equivalent ``/exec-attach``/``/exec`` text
BEFORE its own ``/`` check, then falls straight through the SAME parsing,
echo, and dispatch this module already had — there is no second interpreter,
and the server never sees a bang (by the time anything crosses the transport
it is an ordinary resolved command name + args, identical to what typing
``/exec`` would have produced). ``!!`` is checked first because ``!!cmd``
also satisfies ``text.startswith("!")`` — checking the single-bang form first
would misroute every double-bang line to ``/exec-attach``.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Coroutine

from reyn.interfaces.slash import REGISTRY, SlashContext, suggest_for_unknown
from reyn.interfaces.transport.client_transport import ClientTransport

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import BacklogBatch, Frame
    from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)


def _display(transport: "ClientTransport", kind: str, text: str, **meta) -> None:
    from reyn.runtime.outbox import OutboxMessage
    if kind == "error":
        text = with_control_failure(transport, text)
    transport.put_display(OutboxMessage(kind=kind, text=text, meta=dict(meta)))


def with_control_failure(transport: "ClientTransport", text: str) -> str:
    """#5907 ②: the ONE place a failure line learns why. Appends the typed
    outcome's own wording (``describe_control_failure``) when the
    transport's latest control POST was refused or not delivered — so the
    27 handlers that write ``if not ok: reply_error(...)`` say the right
    thing without being edited, and a timeout and a refusal can never read
    the same. Nothing is appended for a delivered / untyped / wire-less
    transport."""
    from reyn.interfaces.transport.control_outcome import describe_control_failure

    detail = describe_control_failure(transport.last_control_outcome())
    return f"{text} — {detail}" if detail else text


async def maybe_dispatch_slash(
    transport: "ClientTransport", text: str, *, echo: bool = True,
    runner: "Callable[[Coroutine[Any, Any, None]], None] | None" = None,
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

    #5837 stage 2: a leading ``!``/``!!`` is rewritten to ``/exec-attach``/
    ``/exec`` right here, before anything below reads ``text`` — see this
    module's own docstring for why order and placement both matter. The
    ECHOED line is the rewritten ``/exec …`` form, not the original ``!…`` —
    the same choice ``exec.py``'s own ``/exec-attach`` already made for its
    queued block (log what actually ran, not what was typed before
    normalization), applied here for the same "readable after the fact"
    reason. A bare ``!``/``!!`` (no command text) is a mistake worth saying
    so about, not a message to submit as a turn — same shape the bare-``/``
    catalog branch below already gives an empty ``/``.
    """
    if text.startswith("!!"):
        rest = text[2:]
        if not rest.strip():
            if echo:
                _display(transport, "user", text)
            _display(
                transport, "error",
                "!! requires a command; usage: !!<cmdline> (same as /exec <cmdline>)",
            )
            return True
        text = "/exec " + rest
    elif text.startswith("!"):
        rest = text[1:]
        if not rest.strip():
            if echo:
                _display(transport, "user", text)
            _display(
                transport, "error",
                "! requires a command; usage: !<cmdline> (same as /exec-attach <cmdline>)",
            )
            return True
        text = "/exec-attach " + rest

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
    cmd = REGISTRY.get(name)
    if cmd is None:
        # Suggest the 3 closest matches rather than dumping the full catalog:
        # the full list used to truncate mid-name, hiding the actionable
        # suggestions. ``kind="error"`` so the TUI renders an inline error — a
        # ``system`` line is indistinguishable from a successful reply, which
        # made a typo'd command silently look OK.
        known = ", ".join(f"/{n}" for n in suggest_for_unknown(name))
        _display(transport, "error", f"unknown command /{name}; try: {known}")
        return True

    # #5096 ②, architect ruling (issuecomment-5379623427/5379638878/
    # 5379657592): locus decides WHERE the SlashContext is built, not HOW
    # to dispatch. "session" locus is unchanged (forward to wherever the
    # session actually is). "client"/"connection" locus commands need
    # NEITHER a real session NOR a forward — this layer builds the
    # SlashContext itself, right here, with the CLIENT's own transport and
    # session=None, and executes immediately. This is the fix for the
    # owner-reported "attach coder-smith failed" over --connect: /attach
    # used to forward to generic server-side slash dispatch, landing on
    # SessionBoundTransport (send-side only, structurally unable to
    # answer "attach a different agent") instead of ever reaching
    # AgUiTransport's own correctly-implemented request_attach.
    locus = cmd.locus(args) if callable(cmd.locus) else cmd.locus

    async def _run() -> None:
        # #5907 ①: the run UNIT — the one place a command touches the wire
        # (a session-locus command is one control POST; a connection-locus
        # / client-locus handler awaits its own transport call inside).
        # Handed to ``runner`` when the caller has one, so a UI's message
        # pump never awaits it; awaited inline otherwise (the plain CUI's
        # input loop is not a pump).
        if locus == "session":
            ran = await transport.run_slash_command(name, args)
            if not ran:
                # #6083: this branch's own ``ran`` comes back from a REAL
                # control POST (`run_slash_command`), so ``False`` does NOT
                # mean "this client has no session" — that claim was
                # hardcoded here regardless of cause, and the owner's own
                # real-machine report shows it firing on a compaction that
                # had ALREADY SUCCEEDED server-side.
                #
                # lead-coder BLOCKING (PR #6094, self-caught in review): an
                # earlier version of this fix still asserted "could not
                # run" unconditionally, appending the real detail only as a
                # SUFFIX — a reader reads left to right and classifies on
                # the FIRST assertion, so "could not run: <claim>. —
                # <detail>" still reads as the claim, the detail merely
                # supporting it, never correcting it. ``ControlOutcome``'s
                # own two failure kinds say DIFFERENT things and must not
                # share a prefix: ``refused`` genuinely means the server
                # said no — it did not run, and the text may say so.
                # ``not_delivered`` means UNCONFIRMED, not unrun — #6083's
                # own report is exactly this case (a control-read timeout
                # on an operation that ran to completion regardless), so
                # THIS prefix must not claim non-execution either.
                from reyn.interfaces.transport.control_outcome import ControlOutcome

                outcome = transport.last_control_outcome()
                if isinstance(outcome, ControlOutcome) and outcome.kind == "refused":
                    prefix = f"/{name} could not run"
                else:
                    # not_delivered, or no typed outcome recorded — neither
                    # confirms non-execution, so this text does not claim
                    # it. ⚠️ This alone does not fix "a SUCCESS is reported
                    # as a failure" (the operation itself is not confirmed
                    # either way here) — see #6083's own PR body for stage
                    # 2 (an immediate accept + completion reported over the
                    # existing SSE broadcast, the same channel compaction's
                    # own lifecycle markers already use).
                    prefix = f"/{name}: no confirmation received"
                # #6085 stage 2 (lead-coder ruling): this line deliberately
                # carries NO ``compaction_episode_marker`` meta, even when
                # ``name == "compact"`` — unlike this session's compaction
                # markers do NOT run through the shared derivation
                # mechanism, folding this into the same open flow entry.
                # This code is entirely CLIENT-side (the #3595 S5 boundary:
                # a client interprets, ``Session`` never interprets a
                # string, so nothing here ever touches ``Session`` or its
                # episode-seq counter) — a marker with no real seq would
                # be inert under app.py's seq-equality absorption check,
                # and threading the seq back across the control-response
                # wire just to fold THIS one line was judged not worth
                # crossing that boundary for. #6100 (⑵-b) is expected to
                # make this branch fire far less often for `/compact`
                # specifically, but that is not assumed here — this stays
                # its own, unabsorbed line regardless of whether it still
                # fires.
                _display(transport, "error", prefix)
        else:
            ctx = SlashContext(transport=transport, session=None)
            ran = await execute_slash_command(ctx, name, args)
            if not ran:
                # This branch's own ``ran`` comes back from a LOCAL,
                # in-process call (`execute_slash_command`, no control POST
                # at all — see the ``locus`` comment above) — `False` here
                # genuinely does mean "this client has no session to run it
                # on" (the client/connection-locus handler's own precondition
                # check), unchanged from before #6083.
                _display(
                    transport, "error",
                    f"/{name} could not run: this client has no session to run it on.",
                )

    if runner is not None:
        runner(_run())
        return True
    await _run()
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

    def frames(self) -> "AsyncIterator[Frame | BacklogBatch]":
        return self._inner.frames()

    def has_session(self) -> bool:
        return self._inner.has_session()

    def attach_failed(self) -> bool:
        return self._inner.attach_failed()

    def last_control_outcome(self):
        return self._inner.last_control_outcome()

    def pending_intervention_head(self) -> "object | None":
        return self._inner.pending_intervention_head()

    async def clear_pending_command_ui(self) -> None:
        await self._inner.clear_pending_command_ui()

    def reyn_state_root(self) -> "Path | None":
        return self._inner.reyn_state_root()

    async def submit_user_text(
        self, text: str, *, client_ref: "str | None" = None,
    ) -> str:
        return await self._inner.submit_user_text(text, client_ref=client_ref)

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

    async def request_mcp_retry(self, server: str) -> bool:
        return await self._inner.request_mcp_retry(server)

    async def request_attach(self, agent_name: str) -> bool:
        return await self._inner.request_attach(agent_name)

    async def request_session_switch(self, session_id: str) -> bool:
        return await self._inner.request_session_switch(session_id)

    async def request_artifact_list(
        self, *, agent: str
    ) -> "tuple[list[dict], int]":
        return await self._inner.request_artifact_list(agent=agent)

    async def request_session_list(self) -> "list[dict]":
        return await self._inner.request_session_list()

    async def request_older_backlog(self, before_root_id: str) -> None:
        await self._inner.request_older_backlog(before_root_id)

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
