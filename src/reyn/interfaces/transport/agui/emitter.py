"""Server-side AG-UI emitter — a reyn ``Frame`` stream → SSE text (ADR-0039 P2).

The single-writer server holds the session; a remote client attaches over
HTTP+SSE. This emitter is the server half of that wire: it consumes the SAME
unified ``Frame`` stream the local :class:`~reyn.interfaces.transport.in_process.InProcessTransport`
produces (display outbox + renderer-relevant audit-events) and serializes it to
AG-UI SSE via :mod:`reyn.interfaces.transport.agui.protocol`. Because both
transports feed off the identical frame source, *local ≡ remote by construction*
(D2) — the emitter adds only wire framing, never new render semantics.

On connect it replays the reconnect snapshots (A4): ``MESSAGES_SNAPSHOT`` (the
display backlog) then ``STATE_SNAPSHOT`` (the status read-model).

**Session-switch parity (#3310 N3).** A session switch is treated as a
*logical reconnect*: right after a ``session_attached`` ``EventFrame`` (#3310 N1,
carrying ``{agent, session_id}``) is forwarded on the wire, this emitter
re-fires the SAME reconnect protocol — ``MESSAGES_SNAPSHOT`` (the NEW
session's backlog, resolved via the caller-supplied ``backlog_provider``) then
``STATE_SNAPSHOT`` — so connect-time and switch-time are one code path
(:meth:`_reconnect_snapshot_chunks`). Without this, a remote client has no way
to obtain a switched-to session's scrollback at all: the read-model's
``conversation_history`` is deliberately empty for a remote client
(frame-sufficiency, ``read_model.py``), and this emitter's own backlog is
otherwise fixed at connection time. The barrier ordering is load-bearing: the
re-fire happens strictly AFTER the ``session_attached`` frame is forwarded
(never before), so a client that resets its view on that barrier never has the
reset race the very state this re-fire just delivered. The per-connection
``TextStreamTracker`` and ``waiting_on`` label are reset at the same point — a
new session owns neither the old one's in-flight streamed-text bracketing nor
its WaitingOn state.

It then streams each frame as its AG-UI **wire sequence** (:func:`encode_frame_wire_streaming` —
a whole text message is the canonical ``TEXT_MESSAGE_START`` → ``…_CONTENT`` →
``…_END`` triplet, P4; a message that streamed (#3288 ③b/③d, ``agent_delta``
audit-events) instead gets a REAL multi-CONTENT sequence — one ``TEXT_MESSAGE_START``
at the first delta, one ``TEXT_MESSAGE_CONTENT`` per delta, one ``TEXT_MESSAGE_END``
at completion carrying the completion's full text — via a ``TextStreamTracker``
this emitter owns, scoped to THIS connection; every other frame is a single
event), and after each frame emits a ``STATE_DELTA`` when the projected status
changed — the current WaitingOn label is tracked off the audit-event stream so the
remote status panel follows Thinking / Running / Waiting-for-you without a
second source.
"""
from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable

from reyn.interfaces.transport.agui.protocol import (
    CONTROL_FILTER_KINDS,
    TextStreamTracker,
    encode_frame_wire_streaming,
    encode_intervention_tool_result,
    encode_intervention_tool_start,
    encode_messages_snapshot,
    encode_state_delta,
    encode_state_snapshot,
    to_sse,
)
from reyn.interfaces.transport.agui.state import StatusModel, project_status
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, Frame

# WaitingOn label derivation off the audit-event stream — a lightweight, local
# mirror of the renderer's ``_WAITING_ON_BY_EVENT`` table + turn lifecycle, kept
# here so the emitter need not import the inline app (which pulls the renderer).
# turn_settled/completed/cancelled → idle (None); tool_called → Running <tool>.
_IDLE_EVENTS = frozenset({"turn_settled", "turn_completed", "turn_cancelled"})


def _waiting_on_after(etype: str, edata: dict, current: "str | None") -> "str | None":
    if etype == "turn_started":
        return "Thinking"
    if etype == "tool_called":
        tool = edata.get("tool")
        return f"Running {tool}" if tool else "Running"
    if etype in ("tool_returned", "tool_failed"):
        return "Thinking"
    if etype in _IDLE_EVENTS:
        return None
    return current


class AgUiEmitter:
    """Serialize a reyn ``Frame`` stream (+ status read-model) to AG-UI SSE text."""

    def __init__(
        self,
        frames: "AsyncIterator[Frame]",
        status_provider: "Callable[[], dict | None]",
        *,
        backlog: "list[Frame] | None" = None,
        backlog_has_more: bool = False,
        backlog_next_cursor: "str | None" = None,
        backlog_provider: "Callable[[str, str], Awaitable[tuple[list[Frame], bool, str | None]]] | None" = None,
    ) -> None:
        # ``frames`` is the unified frame stream (e.g. an InProcessTransport's
        # ``frames()``); ``status_provider`` returns the CUI status snapshot dict
        # (or None when no session is attached); ``backlog`` is the display
        # history replayed on connect for reconnect (A4), ALREADY bounded to one
        # page by the caller (#5139 C — this class never pages on its own);
        # ``backlog_has_more``/``backlog_next_cursor`` are that same page's own
        # continuation state, encoded alongside it. ``backlog_provider``
        # (#3310 N3) is called with ``(agent, session_id)`` off a mid-stream
        # ``session_attached`` ``EventFrame`` to fetch the switched-to session's
        # OWN bounded page (``(frames, has_more, next_cursor)``, #5139 C) for
        # the re-fire; ``None`` means this connection never switches sessions
        # (byte-identical to pre-N3 behavior).
        self._frames = frames
        self._status_provider = status_provider
        self._backlog = list(backlog or [])
        self._backlog_has_more = backlog_has_more
        self._backlog_next_cursor = backlog_next_cursor
        self._backlog_provider = backlog_provider
        self._model = StatusModel()
        self._waiting_on: str | None = None
        # #3288 ③d: one tracker per connection so a streamed reply's
        # START/END bracketing reflects exactly what THIS connection
        # personally observed (the late-joiner-closing mechanism — see
        # ``TextStreamTracker``'s docstring, protocol.py).
        self._text_stream = TextStreamTracker()

    def _project(self) -> dict:
        return project_status(self._status_provider(), waiting_on=self._waiting_on)

    def _reconnect_snapshot_chunks(
        self, backlog: "list[Frame]", *, has_more: bool = False, next_cursor: "str | None" = None,
    ) -> "list[str]":
        """The shared reconnect protocol (A4): backlog display, then full
        status — used identically at connect (``stream()`` start) and at a
        mid-stream session switch (#3310 N3), so the two are ONE code path,
        not a byte-identical-by-hand duplicate. ``has_more``/``next_cursor``
        (#5139 C) are *backlog*'s own page-continuation state, riding the
        SAME ``MESSAGES_SNAPSHOT`` event."""
        return [
            to_sse(encode_messages_snapshot(backlog, has_more=has_more, next_cursor=next_cursor)),
            to_sse(encode_state_snapshot(self._model.snapshot(self._project()))),
        ]

    async def stream(self) -> AsyncIterator[str]:
        # Reconnect snapshots first (A4): backlog display, then full status.
        for chunk in self._reconnect_snapshot_chunks(
            self._backlog, has_more=self._backlog_has_more, next_cursor=self._backlog_next_cursor,
        ):
            yield chunk

        async for frame in self._frames:
            # Control sentinels in CONTROL_FILTER_KINDS are NOT forwarded on the
            # AG-UI wire — the explicit per-entry allowlist (protocol.py), not the
            # negation of any forward-set. It holds only ``__end__`` (the stream
            # terminator; returns below) and ``__open_artifact__`` (local-only by
            # construction). Client-consumed sentinels ``__copy_last_reply__`` /
            # ``__rewind_list__`` are DELIBERATELY NOT here: the client consumes
            # them over the transport stream (real clipboard copy / rewind
            # picker), so they are forwarded as profiled CUSTOM events —
            # filtering them would make remote /copy / /rewind silent no-ops.
            is_control = (
                isinstance(frame, DisplayFrame)
                and frame.message.kind in CONTROL_FILTER_KINDS
            )
            if is_control:
                if frame.message.kind == "__end__":
                    return
                continue
            # A whole text message expands to the AG-UI START→CONTENT→END triplet
            # (conformance); a message that streamed (#3288 ③d) instead gets a
            # real multi-CONTENT sequence via the per-connection tracker; every
            # other frame is a single event. Only the _reyn-bearing event(s)
            # round-trip to the reyn client — non-reconstructing START/END are
            # generic scaffold. An ``intervention`` kind is CUSTOM (a single
            # event), so the triplet never disturbs the frontend-tool path below.
            for event in encode_frame_wire_streaming(frame, self._text_stream):
                yield to_sse(event)
            # HITL frontend-tool lifecycle (ADR-0039 P3, R4). An intervention
            # rides the wire in TWO representations: the DisplayFrame above (the
            # reyn client's native prompt UI) AND a companion frontend-tool
            # TOOL_CALL_START — the generic-client render + the answer-correlation
            # anchor (toolCallId = intervention id, R1). On answer we emit the
            # terminal TOOL_CALL_RESULT so a pending frontend-tool never dangles.
            if isinstance(frame, DisplayFrame):
                msg = frame.message
                # #5047: the `and (msg.meta or {}).get("intervention_id")`
                # this line used to carry is now redundant — dropped, not
                # merely simplified: `OutboxMessage.__post_init__` requires
                # a genuine `intervention_id` for every `kind=="intervention"`
                # frame at construction time, so a frame reaching here with
                # that kind is guaranteed to carry one. This is itself the
                # observation point that the fix landed.
                if msg.kind == "intervention":
                    yield to_sse(encode_intervention_tool_start(dict(msg.meta)))
            if isinstance(frame, EventFrame):
                ev = frame.event
                etype = getattr(ev, "type", "") or ""
                edata = dict(getattr(ev, "data", {}) or {})
                if etype == "user_answered_intervention" and edata.get("intervention_id"):
                    yield to_sse(
                        encode_intervention_tool_result(edata["intervention_id"], "answered")
                    )
                self._waiting_on = _waiting_on_after(etype, edata, self._waiting_on)
                # #3310 N3: logical-reconnect re-fire, STRICTLY after the
                # session_attached frame was forwarded above (never before —
                # the barrier ordering is the whole point: a client resets its
                # view on that barrier, and must never see the reset race the
                # state this re-fire is about to deliver). A new session owns
                # neither the old one's in-flight streamed-text bracketing nor
                # its WaitingOn label, so both are reset before the re-fire.
                if etype == "session_attached" and self._backlog_provider is not None:
                    self._text_stream = TextStreamTracker()
                    self._waiting_on = None
                    new_backlog, new_has_more, new_next_cursor = await self._backlog_provider(
                        str(edata.get("agent", "")), str(edata.get("session_id", ""))
                    )
                    for chunk in self._reconnect_snapshot_chunks(
                        new_backlog, has_more=new_has_more, next_cursor=new_next_cursor,
                    ):
                        yield chunk
            delta = self._model.delta(self._project())
            if delta:
                yield to_sse(encode_state_delta(delta))


__all__ = ["AgUiEmitter"]
