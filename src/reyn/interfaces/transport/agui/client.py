"""Client-side ``AgUiTransport`` — the SECOND ClientTransport, over the wire (P2).

P1 proved the :class:`~reyn.interfaces.transport.client_transport.ClientTransport`
seam with the local :class:`~reyn.interfaces.transport.in_process.InProcessTransport`.
P2 adds this remote sibling: it decodes a server AG-UI SSE stream back into the
SAME :class:`~reyn.interfaces.transport.frames.Frame` objects the renderer
consumes — so ``reyn chat --connect`` drives the identical renderer, a different
transport (D2). renderer and stream_client are UNCHANGED.

The transport is decoupled from HTTP for testability and single-responsibility:
it consumes an ``AsyncIterator[str]`` of raw SSE lines (production: an httpx
``aiter_lines`` over the SSE endpoint) and calls an injected ``send`` coroutine
to POST a client→server message (production: an httpx POST). :meth:`frames`
demuxes the one SSE stream three ways — render frames to the renderer, STATE_*
to the :class:`~reyn.interfaces.transport.agui.state.RemoteStatusView`
side-channel, and the reconnect MESSAGES/STATE snapshots — while re-guarding
presentation nodes at the edge (A5).

Phase boundary: P2 is DISPLAY + basic turn submit only. The intervention-answer
methods exist to satisfy the ABC but are the HITL answer round-trip = P3; here
they best-effort POST and the client does not introspect the server's
intervention queue (:meth:`pending_intervention_head` is ``None``), so
``route_input_line`` always takes the ordinary turn-submit path.
"""
from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable

from reyn.interfaces.transport.agui.protocol import (
    MessagesSnapshot,
    StateUpdate,
    decode_event,
    parse_sse_blocks,
)
from reyn.interfaces.transport.agui.state import RemoteStatusView, reguard_nodes
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, Frame


class AgUiTransport(ClientTransport):
    """Decode a server AG-UI SSE stream into the renderer's ``Frame`` vocabulary."""

    def __init__(
        self,
        sse_lines: "AsyncIterator[str]",
        send: "Callable[[dict], Awaitable[None]]",
        *,
        status_view: "RemoteStatusView | None" = None,
        reguard_surface: str = "terminal",
        connected: bool = True,
    ) -> None:
        self._sse_lines = sse_lines
        self._send = send
        self._status = status_view if status_view is not None else RemoteStatusView()
        self._reguard_surface = reguard_surface
        self._connected = connected

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # The SSE line source is created and owned by the caller (the httpx
        # connect happens before construction); nothing to wire up here.
        return None

    def close(self) -> None:
        self._connected = False

    # -- status side-channel ------------------------------------------------

    @property
    def status(self) -> RemoteStatusView:
        """The remote status read-model view (reflects the server's STATE_*)."""
        return self._status

    # -- frame production ---------------------------------------------------

    def _reguard_frame(self, frame: Frame) -> Frame:
        # Per-connection edge re-guard for presentation render-nodes (A5): inert
        # at construction already, re-neutralized here for a heterogeneous client.
        if isinstance(frame, DisplayFrame) and frame.message.kind == "presentation":
            nodes = frame.message.meta.get("nodes")
            if isinstance(nodes, list):
                meta = dict(frame.message.meta)
                meta["nodes"] = reguard_nodes(nodes, surface=self._reguard_surface)
                from reyn.runtime.outbox import OutboxMessage  # noqa: PLC0415

                return DisplayFrame(
                    OutboxMessage(kind="presentation", text=frame.message.text, meta=meta)
                )
        return frame

    def _consume_block(self, block_lines: "list[str]") -> "list[Frame]":
        # Decode one SSE block into zero or more render frames, routing STATE_*
        # and MESSAGES_SNAPSHOT to their side-channels.
        out: list[Frame] = []
        for ev in parse_sse_blocks(block_lines + [""]):
            decoded = decode_event(ev.type, ev.data)
            if decoded is None:
                continue  # foreign / unknown event — ignore-unknown (D6)
            if isinstance(decoded, StateUpdate):
                if decoded.snapshot is not None:
                    self._status.apply_snapshot(decoded.snapshot)
                if decoded.delta is not None:
                    self._status.apply_delta(decoded.delta)
            elif isinstance(decoded, MessagesSnapshot):
                out.extend(self._reguard_frame(f) for f in decoded.frames)
            else:  # a Frame
                out.append(self._reguard_frame(decoded))
        return out

    async def frames(self) -> "AsyncIterator[Frame]":
        block: list[str] = []
        async for raw in self._sse_lines:
            line = raw.rstrip("\n")
            if line == "":
                if block:
                    for frame in self._consume_block(block):
                        yield frame
                        if (
                            isinstance(frame, DisplayFrame)
                            and frame.message.kind == "__end__"
                        ):
                            return
                    block = []
                continue
            block.append(line)
        # Flush a trailing block with no terminal blank line.
        if block:
            for frame in self._consume_block(block):
                yield frame

    # -- send side ----------------------------------------------------------

    def has_session(self) -> bool:
        return self._connected

    def pending_intervention_head(self) -> "object | None":
        # P2 is display-only for interventions; the client does not introspect
        # the server's intervention queue (that is P3), so input always takes
        # the ordinary turn-submit path in route_input_line.
        return None

    async def submit_user_text(self, text: str) -> None:
        await self._send({"type": "user_message", "text": text})

    async def answer_intervention_text(self, text: str) -> bool:
        # P3 (HITL answer round-trip). Best-effort POST for forward-wiring; P2
        # never routes here because pending_intervention_head() is None.
        await self._send({"type": "answer_intervention", "text": text})
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        await self._send({"type": "answer_intervention", "text": choice_id})
        return False

    def put_display(self, msg) -> None:
        # A remote client cannot inject into the server's outbox; client-authored
        # echoes render locally through the renderer, not this seam. No-op here.
        return None

    async def cancel_inflight(self) -> None:
        await self._send({"type": "cancel_inflight"})

    async def shutdown(self) -> None:
        self._connected = False
        await self._send({"type": "shutdown"})


__all__ = ["AgUiTransport"]
