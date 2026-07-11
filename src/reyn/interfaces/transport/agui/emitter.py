"""Server-side AG-UI emitter — a reyn ``Frame`` stream → SSE text (ADR-0039 P2).

The single-writer server holds the session; a remote client attaches over
HTTP+SSE. This emitter is the server half of that wire: it consumes the SAME
unified ``Frame`` stream the local :class:`~reyn.interfaces.transport.in_process.InProcessTransport`
produces (display outbox + renderer-relevant chat-events) and serializes it to
AG-UI SSE via :mod:`reyn.interfaces.transport.agui.protocol`. Because both
transports feed off the identical frame source, *local ≡ remote by construction*
(D2) — the emitter adds only wire framing, never new render semantics.

On connect it replays the reconnect snapshots (A4): ``MESSAGES_SNAPSHOT`` (the
display backlog) then ``STATE_SNAPSHOT`` (the status read-model). It then streams
one SSE event per frame, and after each frame emits a ``STATE_DELTA`` when the
projected status changed — the current WaitingOn label is tracked off the
chat-event stream so the remote status panel follows Thinking / Running /
Waiting-for-you without a second source.
"""
from __future__ import annotations

from typing import AsyncIterator, Callable

from reyn.interfaces.transport.agui.protocol import (
    encode_frame,
    encode_messages_snapshot,
    encode_state_delta,
    encode_state_snapshot,
    to_sse,
)
from reyn.interfaces.transport.agui.state import StatusModel, project_status
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, Frame

# WaitingOn label derivation off the chat-event stream — a lightweight, local
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
    ) -> None:
        # ``frames`` is the unified frame stream (e.g. an InProcessTransport's
        # ``frames()``); ``status_provider`` returns the CUI status snapshot dict
        # (or None when no session is attached); ``backlog`` is the display
        # history replayed on connect for reconnect (A4).
        self._frames = frames
        self._status_provider = status_provider
        self._backlog = list(backlog or [])
        self._model = StatusModel()
        self._waiting_on: str | None = None

    def _project(self) -> dict:
        return project_status(self._status_provider(), waiting_on=self._waiting_on)

    async def stream(self) -> AsyncIterator[str]:
        # Reconnect snapshots first (A4): backlog display, then full status.
        yield to_sse(encode_messages_snapshot(self._backlog))
        yield to_sse(encode_state_snapshot(self._model.snapshot(self._project())))

        async for frame in self._frames:
            yield to_sse(encode_frame(frame))
            if isinstance(frame, EventFrame):
                ev = frame.event
                self._waiting_on = _waiting_on_after(
                    getattr(ev, "type", "") or "", dict(getattr(ev, "data", {}) or {}),
                    self._waiting_on,
                )
            delta = self._model.delta(self._project())
            if delta:
                yield to_sse(encode_state_delta(delta))
            if isinstance(frame, DisplayFrame) and frame.message.kind == "__end__":
                return


__all__ = ["AgUiEmitter"]
