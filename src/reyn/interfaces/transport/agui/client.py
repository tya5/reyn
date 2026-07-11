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

P3 (HITL answer round-trip): the client tracks the pending intervention BY ID
off the intervention frontend-tool (:class:`InterventionTool`) the server emits
alongside the display frame, and an operator line is delivered to THAT id via a
``TOOL_CALL_RESULT`` POST (:meth:`answer_intervention_text` /
:meth:`answer_intervention_choice`) — never answer-oldest (R1). The frontend-tool
is used ONLY for answer-correlation; the prompt is drawn from the P2 DisplayFrame,
so there is no double-render. ``shutdown`` is a **client-local disconnect only** —
a client can NEVER tear down the single-writer server (that closes the ws
footgun where a client kills the server).
"""
from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable

from reyn.interfaces.transport.agui.protocol import (
    InterventionTool,
    InterventionToolResult,
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
        send: "Callable[[dict], Awaitable[bool | None]]",
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
        # The intervention currently awaiting an answer (its id = the
        # TOOL_CALL_RESULT toolCallId, P3/R1). Set when the server emits the
        # intervention frontend-tool; cleared when it resolves (answered / DENY).
        self._pending_intervention_id: "str | None" = None

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
            elif isinstance(decoded, InterventionTool):
                # Answer-correlation only (R4-ii): record which intervention is
                # pending so an operator line routes to it BY ID (R1). NOT a
                # render frame — the prompt is drawn from the DisplayFrame.
                self._pending_intervention_id = decoded.intervention_id or None
            elif isinstance(decoded, InterventionToolResult):
                # Terminal (answered / DENY): the pending frontend-tool resolved.
                if decoded.intervention_id == self._pending_intervention_id:
                    self._pending_intervention_id = None
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
        # P3: the id of the intervention awaiting an answer, tracked off the
        # server's intervention frontend-tool. Non-None routes an operator line
        # to answer_intervention_text (delivered BY ID, R1) instead of a new turn.
        return self._pending_intervention_id

    async def submit_user_text(self, text: str) -> None:
        await self._send({"type": "user_message", "text": text})

    async def answer_intervention_text(self, text: str) -> bool:
        # P3 HITL answer: POST a TOOL_CALL_RESULT correlated to the pending
        # intervention BY ID (toolCallId, R1). The server re-authorizes at
        # delivery (identity + active-driver) and resolves by id; a rejected or
        # unroutable answer returns False so the caller falls back to a turn.
        iv_id = self._pending_intervention_id
        if iv_id is None:
            return False
        return await self._post_answer(iv_id, text=text)

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        iv_id = self._pending_intervention_id
        if iv_id is None:
            return False
        return await self._post_answer(iv_id, choice_id=choice_id)

    async def _post_answer(
        self, intervention_id: str, *, text: str = "", choice_id: str | None = None
    ) -> bool:
        # The client echoes ONLY (toolCallId, text|choiceId) — the server
        # validates against its own registry entry; the client's copy of the
        # prompt/choices is not trusted (R6). ``send`` returns the server's
        # accepted/rejected verdict (True iff the grant was delivered).
        payload: dict = {"type": "TOOL_CALL_RESULT", "toolCallId": intervention_id}
        if choice_id is not None:
            payload["choiceId"] = choice_id
        else:
            payload["text"] = text
        accepted = await self._send(payload)
        if accepted:
            # The grant landed — consume the local correlation so a follow-up
            # line is a fresh turn (the server's terminal TOOL_CALL_RESULT, which
            # arrives on a later frame, is then a no-op for this client).
            if self._pending_intervention_id == intervention_id:
                self._pending_intervention_id = None
        return bool(accepted)

    def put_display(self, msg) -> None:
        # A remote client cannot inject into the server's outbox; client-authored
        # echoes render locally through the renderer, not this seam. No-op here.
        return None

    async def cancel_inflight(self) -> None:
        await self._send({"type": "cancel_inflight"})

    async def shutdown(self) -> None:
        # Client-LOCAL disconnect only (A3). A client can never tear down the
        # single-writer server — no shutdown is sent over the wire (closing the
        # ws footgun where a client kills the server). Just drop the connection.
        self._connected = False


__all__ = ["AgUiTransport"]
