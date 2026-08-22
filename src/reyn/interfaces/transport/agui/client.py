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

import asyncio
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
from reyn.interfaces.transport.drain import suspend_between_frames
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, Frame


class AgUiTransport(ClientTransport):
    """Decode a server AG-UI SSE stream into the renderer's ``Frame`` vocabulary."""

    def __init__(
        self,
        sse_lines: "AsyncIterator[str]",
        send: "Callable[[dict], Awaitable[dict | None]]",
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
        # #5050 ③: set once the FIRST STATE_SNAPSHOT (always the first thing
        # the reconnect protocol sends — ``AgUiEmitter.stream``'s own
        # ordering) has been decoded into ``self._status`` — see
        # :meth:`state_ready`'s own docstring (on the base class) for why
        # this is a separate axis from :meth:`frames`.
        self._state_ready_event = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # The SSE line source is created and owned by the caller (the httpx
        # connect happens before construction); nothing to wire up here.
        return None

    async def state_ready(self) -> None:
        await self._state_ready_event.wait()

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
                # #5050 ③: either kind of STATE_* update means the status
                # side-channel now reflects at least one genuine server
                # update — see :meth:`state_ready`'s own docstring for why
                # this is set here, independent of whether this block also
                # yields any display Frame.
                self._state_ready_event.set()
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
                # #5050 ③ (architect co-vet, issuecomment-5377613210 — a
                # correction of this PR's OWN first draft): a
                # ``session_attached`` EventFrame starts a NEW episode —
                # ``AgUiEmitter``'s reconnect-protocol barrier
                # (``emitter.py``'s own module docstring: the STATE_SNAPSHOT
                # re-fire happens STRICTLY AFTER the ``session_attached``
                # frame is forwarded, never before) means ``self._status``
                # still reflects the OLD session's state at the instant this
                # frame is decoded. Clearing the Event here — rather than
                # leaving the FIRST session's one-shot ``set()`` standing
                # forever — makes a SECOND ``await state_ready()`` after a
                # switch correctly wait for THIS episode's own fresh
                # snapshot instead of resolving immediately on stale data
                # (the "lying-ready" shape architect's original ruling
                # named, now applied per-episode instead of once ever).
                # ``_session_switch_generation`` is unrelated and unchanged
                # (architect: do not invent a second generation mechanism)
                # — this Event answers "has THIS episode's state landed",
                # the generation answers "is this still the latest switch".
                if (
                    isinstance(decoded, EventFrame)
                    and getattr(decoded.event, "type", None) == "session_attached"
                ):
                    self._state_ready_event.clear()
                out.append(self._reguard_frame(decoded))
        return out

    async def frames(self) -> "AsyncIterator[Frame]":
        block: list[str] = []
        async for raw in self._sse_lines:
            line = raw.rstrip("\n")
            if line == "":
                if block:
                    for frame in self._consume_block(block):
                        # #3570, same property as ``InProcessTransport.frames``:
                        # one SSE block decodes to MANY frames (a MESSAGES_SNAPSHOT
                        # reconnect alone carries the whole backlog) and this inner
                        # loop has no await of its own, while ``aiter_lines`` over a
                        # buffered read returns without suspending either. Without
                        # this line whether the loop breathes is a function of how
                        # much the server packed into one block.
                        await suspend_between_frames()
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
                await suspend_between_frames()  # #3570, same reason as above
                yield frame

    # -- send side ----------------------------------------------------------

    def has_session(self) -> bool:
        return self._connected

    def pending_intervention_head(self) -> "object | None":
        # P3: the id of the intervention awaiting an answer, tracked off the
        # server's intervention frontend-tool. Non-None routes an operator line
        # to answer_intervention_text (delivered BY ID, R1) instead of a new turn.
        return self._pending_intervention_id

    async def submit_user_text(self, text: str) -> str:
        # #3287: the server echoes the msg_id it assigned (the SAME
        # correlation id the broadcast user_submitted audit-event carries,
        # #3300 P2a) in the POST's JSON response — see
        # `agui/endpoint.py`'s `user_message` handler. `""` (never a raised
        # exception / None-propagating crash) on any shape the caller can't
        # use — a rejected/failed POST (`_send` returned None) or a legacy/
        # foreign server that doesn't echo the field — so a caller can always
        # treat "no id" with a plain falsy check, same as `""` from
        # `InProcessTransport` when nothing is attached.
        result = await self._send({"type": "user_message", "text": text})
        msg_id = (result or {}).get("msg_id")
        return msg_id if isinstance(msg_id, str) else ""

    async def run_slash_command(self, name: str, args: str) -> bool:
        # #3595 S5: the remote execution side of the shared client-side slash
        # layer. The CLIENT already interpreted the line — what goes on the wire
        # is a typed payload naming a registered command, never the raw text, so
        # no transport re-tests ``startswith("/")`` and the server executes a
        # named operation rather than sniffing a string. The server re-resolves
        # the name against its OWN registry (a client and a server can be
        # running different builds) and answers whether it ran.
        result = await self._send(
            {"type": "slash_command", "name": name, "args": args}
        )
        return bool((result or {}).get("ran"))

    async def request_attach(self, agent_name: str) -> bool:
        # #4534 PR-1/PR-2: same shape as run_slash_command above, a second
        # typed payload alongside it — retires the __attach_request__
        # display-channel sentinel. The server re-resolves agent_name
        # against its own registry.
        result = await self._send({"type": "attach_request", "agent_name": agent_name})
        return bool((result or {}).get("attached"))

    async def request_session_switch(self, session_id: str) -> bool:
        # #4534 PR-1/PR-2b: mirrors request_attach above, retiring
        # __session_switch_request__.
        result = await self._send(
            {"type": "session_switch_request", "session_id": session_id},
        )
        return bool((result or {}).get("switched"))

    async def request_artifact_list(self, *, agent: str) -> "tuple[list[dict], int]":
        # #4494 design C: POST a typed request; the server reads its OWN
        # copy of the durable artifact-ref table (never a client-supplied
        # path) and answers with the current entries. Same shape as
        # ``request_attach``/``request_session_switch`` above. ``agent``
        # is accepted for parity with the ``ClientTransport`` signature —
        # the server's own ``agent_name`` (baked into the endpoint URL at
        # connect time) is what it actually reads against, so this
        # transport does not thread it onto the wire separately.
        #
        # #4601: the server's own entries are already capped — ``total``
        # (the pre-cap count) rides alongside on the wire so this client
        # can disclose "newest N of M" without a second round-trip.
        result = await self._send({"type": "artifact_list_request"})
        entries = (result or {}).get("entries")
        total = (result or {}).get("total")
        return (
            entries if isinstance(entries, list) else [],
            total if isinstance(total, int) else 0,
        )

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        # P3 HITL answer: POST a TOOL_CALL_RESULT correlated to the pending
        # intervention BY ID (toolCallId, R1). The server re-authorizes at
        # delivery (identity + active-driver) and resolves by id; a rejected or
        # unroutable answer returns False so the caller falls back to a turn.
        # #3299 P2: an explicit ``intervention_id`` (the client's own tracked
        # id) is used as-is instead of the single ``_pending_intervention_id``
        # slot — this wire transport still tracks only one frontend-tool at a
        # time (a wider multi-pending wire protocol is out of this PR's scope,
        # confined to ``interfaces/inline/textual_chat/``), but an explicit id
        # is honored rather than silently overridden by the slot.
        iv_id = intervention_id if intervention_id is not None else self._pending_intervention_id
        if iv_id is None:
            return False
        return await self._post_answer(iv_id, text=text)

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        iv_id = intervention_id if intervention_id is not None else self._pending_intervention_id
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

    async def cancel_inflight(self) -> str:
        # #3903: fire-and-forget over the wire (no response frame in this
        # protocol) — this is the Esc/Ctrl+C keyboard path only. /cancel
        # (the slash command) does NOT route through here: ClientTransport.
        # run_slash_command's own docstring says AgUiTransport POSTs the
        # slash command for the SERVER to run via execute_slash_command,
        # which calls Session.cancel_inflight() directly and gets the real
        # summary — this generic string is never what a /cancel reply shows.
        await self._send({"type": "cancel_inflight"})
        return "cancel requested"

    async def cancel_queued(self, msg_id: str) -> bool:
        # #3300 P3 (Y-server) remote parity: POST the cancel-by-id op; the
        # server's response is HTTP-accepted (2xx), not the cancel's own
        # queued/no-op result — the client observes the actual outcome via
        # the server-authoritative `inbox_cancel` audit-event delta (never a
        # client-local "cancel succeeded" inference), same as every other
        # queue-affecting mutation on this transport.
        return bool(await self._send({"type": "cancel_queued", "msg_id": msg_id}))

    async def shutdown(self) -> None:
        # Client-LOCAL disconnect only (A3). A client can never tear down the
        # single-writer server — no shutdown is sent over the wire (closing the
        # ws footgun where a client kills the server). Just drop the connection.
        self._connected = False


__all__ = ["AgUiTransport"]
