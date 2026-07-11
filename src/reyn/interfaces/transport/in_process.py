"""``InProcessTransport`` — the local :class:`ClientTransport` (ADR-0039 P1).

Composes the two EXISTING in-process delivery mechanisms behind the transport
seam, changing *routing* only (delivery is unchanged, behavior byte-identical):

- **Display path** — the registry's per-session outbox forwarder already pumps
  ``session.outbox`` into ``registry.repl_outbox``. This transport drains that
  queue in a background pump task and re-tags each message as a
  :class:`~reyn.interfaces.transport.frames.DisplayFrame` on the unified stream.
- **Event path** — a ``session.chat_events`` subscription (wired via the
  registry's focus-listener binding, so it follows ``/attach``), *filtered to
  the renderer's forward-set* (:func:`renderer_chat_events`), enqueues each
  relevant event as an :class:`~reyn.interfaces.transport.frames.EventFrame` on
  the SAME unified stream.

The client drains one ordered stream (:meth:`frames`) and dispatches by tag —
the local analogue of the single AG-UI/SSE event stream a remote client (P2)
will consume. The send side wraps today's dispatch (``submit_user_text`` /
``answer_oldest_intervention_*`` / ``repl_outbox`` echo / cancel / shutdown) so
the client writes to the world ONLY through this seam (single-writer).

The forward-set is injectable (``forward_events``, defaulting to the derived
renderer vocabulary) purely so the P1 strip-falsify test can drop it and prove
the event path is load-bearing; production always uses the derived default.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, AsyncIterator

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    Frame,
    FrameTag,
    renderer_chat_events,
)

if TYPE_CHECKING:
    from reyn.core.events.events import Event
    from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)


class InProcessTransport(ClientTransport):
    """The local transport: forwarder + filtered chat-events → one frame stream."""

    def __init__(
        self,
        registry: "object",
        *,
        intervention_channel: str,
        forward_events: "frozenset[str] | None" = None,
    ) -> None:
        # ``registry`` is the AgentRegistry (duck-typed here so the transport
        # package carries no runtime import that the client would inherit). It
        # owns ``repl_outbox``, the outbox forwarder, and the focus-listener
        # binding this transport composes.
        self._registry = registry
        self._intervention_channel = intervention_channel
        # DERIVED renderer vocabulary by default; injectable ONLY for the
        # strip-falsify test (an empty set makes the event path vanish → RED).
        self._forward_events = (
            forward_events if forward_events is not None else renderer_chat_events()
        )
        self._frames: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._pump_task: "asyncio.Task | None" = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # Event path: subscribe the FILTERED chat-event callback + the
        # intervention channel via the registry's focus binding (follows
        # /attach). Display path: pump repl_outbox into the unified stream.
        self._registry.bind_focus_listeners(
            on_chat_event=self._forward_chat_event,
            intervention_channel=self._intervention_channel,
        )
        self._pump_task = asyncio.create_task(self._pump_outbox())

    def close(self) -> None:
        # Unwire from the LIVE attached session (handles a switch before quit).
        self._registry.unbind_focus_listeners()
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None

    # -- frame production ---------------------------------------------------

    def _forward_chat_event(self, event: "Event") -> None:
        # Synchronous subscriber (same mechanism as before): enqueue ONLY the
        # renderer-relevant subset onto the unified stream. Non-renderer events
        # are dropped here — the transport carries the renderer's vocabulary,
        # not every chat-event.
        etype = getattr(event, "type", None)
        if etype in self._forward_events:
            self._frames.put_nowait(EventFrame(event))

    async def _pump_outbox(self) -> None:
        # Re-tag the display outbox onto the unified stream, preserving order.
        # Stops after forwarding the ``__end__`` control frame (session shutdown).
        outbox = self._registry.repl_outbox
        while True:
            msg = await outbox.get()
            self._frames.put_nowait(DisplayFrame(msg))
            if msg.kind == "__end__":
                return

    async def frames(self) -> "AsyncIterator[Frame]":
        while True:
            frame = await self._frames.get()
            yield frame
            if frame.tag is FrameTag.DISPLAY and frame.message.kind == "__end__":
                return

    # -- send side ----------------------------------------------------------

    def _attached(self) -> "object | None":
        return self._registry.attached_session()

    def has_session(self) -> bool:
        return self._attached() is not None

    def pending_intervention_head(self) -> "object | None":
        s = self._attached()
        return s.interventions.head() if s is not None else None

    async def submit_user_text(self, text: str) -> None:
        s = self._attached()
        if s is not None:
            await s.submit_user_text(text)

    async def answer_intervention_text(self, text: str) -> bool:
        s = self._attached()
        if s is None:
            return False
        return bool(await s.answer_oldest_intervention_text(text))

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        s = self._attached()
        if s is None:
            return False
        return bool(await s.answer_oldest_intervention_choice(choice_id))

    def put_display(self, msg: "OutboxMessage") -> None:
        # Client-authored display (user echo, /copy result, resolved-answer
        # marker): route into the SAME outbox the forwarder drains so it lands
        # in FIFO order with the session's own output, then re-tagged by the pump.
        self._registry.repl_outbox.put_nowait(msg)

    async def cancel_inflight(self) -> None:
        s = self._attached()
        if s is None:
            return
        cancel_fn = getattr(s, "cancel_inflight", None)
        if callable(cancel_fn):
            await cancel_fn()

    async def shutdown(self) -> None:
        await self._registry.shutdown()


__all__ = ["InProcessTransport"]
