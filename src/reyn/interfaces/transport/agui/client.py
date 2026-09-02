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
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable

from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
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
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame, EventFrame, Frame

if TYPE_CHECKING:
    from pathlib import Path

# #5107: the sentinel :meth:`AgUiTransport._pump_sse` enqueues on its own
# way out (any of its 3 exits — the source running dry, an ``__end__``
# DisplayFrame, or a raised exception) so :meth:`AgUiTransport.frames`
# — which now waits on a shared queue instead of driving the SSE source
# itself — can tell "no more SSE frames are coming" apart from "the queue
# is just momentarily empty" and stop instead of hanging forever. A
# module-level ``object()`` rather than ``None``: a locally-authored
# ``put_display`` call could in principle wrap a falsy/None-ish payload,
# and this must never be confused with one.
_SSE_DONE = object()


class _SSEPumpError:
    """#5126 (lead-coder catch on #5107, issuecomment-5380819283): a real
    exception (e.g. ``httpx.ReadError`` — the connection dying mid-stream)
    used to be indistinguishable from a clean end-of-stream: :meth:`_pump_sse`
    enqueued the SAME :data:`_SSE_DONE` sentinel in its ``finally`` on every
    exit, then let the exception propagate out of the untracked background
    task — where nothing ever awaited it, so it became an asyncio
    "exception was never retrieved" orphan, and :meth:`frames` (seeing only
    ``_SSE_DONE``) returned exactly as if the stream had ended normally.

    This wraps a genuine (non-cancellation) exception so it travels through
    the SAME queue :data:`_SSE_DONE` does — the one channel a consumer
    (:meth:`frames`) actually drains — instead of being silently dropped on
    the task's own way out. :meth:`frames` re-raises it, so a real caller
    (the TUI, or a test) sees the actual failure instead of a quiet, ordinary
    end."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class AgUiTransport(ClientTransport):
    """Decode a server AG-UI SSE stream into the renderer's ``Frame`` vocabulary."""

    def __init__(
        self,
        sse_lines: "AsyncIterator[str]",
        send: "Callable[[dict], Awaitable[dict | None]]",
        *,
        agent_name: str = "",
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
        # #5139 (architect FINAL ruling, issuecomment-5383272756 —
        # supersedes an earlier side-channel draft that routed
        # ``MessagesSnapshot`` alongside ``StateUpdate`` above; see
        # :meth:`_consume_block`'s own comment on the ``MessagesSnapshot``
        # branch for why that draft was reverted): the destination a
        # backlog batch about to be built is FOR — updated whenever a
        # ``session_attached`` announce decodes (a mid-stream SWITCH re-
        # fire, ``emitter.py``'s N3 path). The VERY FIRST connect's own
        # backlog is a special case this ``session_attached`` decode does
        # NOT cover: ``AgUiEmitter.stream``'s own initial reconnect chunks
        # (``_reconnect_snapshot_chunks``, called before the ``self._frames``
        # loop even starts) are NOT preceded by a ``session_attached``
        # EventFrame on the wire at all — only the N3 mid-stream re-fire
        # goes through ``self._frames`` where that event lives (verified
        # against ``emitter.py``'s own ``stream`` body, not assumed from
        # its module docstring's "one code path" framing, which describes
        # the BACKLOG-BUILDING code path server-side, not the wire framing
        # this client sees). So the FIRST connect's destination is seeded
        # here instead, from what the caller already knows before this
        # transport ever decodes a single SSE line: ``agent_name`` (the
        # URL this connection was opened against) and the well-known
        # default session id — a fresh, never-switched connection is
        # always attached to it (``registry.py``'s own ``_DEFAULT_SID``).
        from reyn.runtime.registry import _DEFAULT_SID  # noqa: PLC0415

        self._backlog_agent = agent_name
        self._backlog_sid = _DEFAULT_SID
        # #5107 (architect ruling B, issuecomment-5379950484): the merge
        # point between two frame producers — the SSE pump (server-sent
        # display, below) and :meth:`put_display` (CLIENT-authored display:
        # a slash reply, an echo, an unknown-command note). Both feed the
        # SAME queue :meth:`frames` drains, so a locally-authored message
        # renders through the identical renderer path a server-sent one
        # does, without waiting on the next SSE event to unblock it.
        self._display_queue: "asyncio.Queue[Frame | BacklogBatch | object]" = asyncio.Queue()
        self._sse_pump_task: "asyncio.Task[None] | None" = None
        # #5694: set once :meth:`frames` re-raises a genuine ``_SSEPumpError``
        # (the pump died — a real read failure, never a clean end or an
        # intentional :meth:`close`). This is the ONE fact ``has_session``/
        # ``attach_failed`` read below to make the loss of this connection's
        # OWN receive channel visible — see those methods' own docstrings
        # for why a dead pump must degrade the SAME tri-state the header and
        # the composer submit-gate (#3671 P3) already share, rather than
        # leaving this transport looking "still attached" while the
        # server's own per-connection binding (``_CONNECTION_RETARGET_HUB``,
        # #5116) has already forgotten it.
        self._pump_died = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # The SSE line source is created and owned by the caller (the httpx
        # connect happens before construction); nothing to wire up here.
        return None

    async def state_ready(self) -> None:
        await self._state_ready_event.wait()

    def close(self) -> None:
        self._connected = False
        # #5107: the SSE pump (started lazily by :meth:`frames`) is a
        # background task now, not inline in the caller's own loop — it
        # must be told to stop rather than leaking past this transport's
        # own lifetime (most visible in tests: many short-lived transports
        # constructed in one process).
        if self._sse_pump_task is not None:
            self._sse_pump_task.cancel()

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

    def _consume_block(self, block_lines: "list[str]") -> "list[Frame | BacklogBatch]":
        # Decode one SSE block into zero or more render frames, routing
        # STATE_* to its side-channel and MESSAGES_SNAPSHOT into `out` as
        # ONE BacklogBatch item (#5139) — everything this transport
        # produces flows through the SAME queue/`frames()` stream, no
        # second channel.
        out: list[Frame | BacklogBatch] = []
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
                # #5139 (architect FINAL ruling, issuecomment-5383272756):
                # ONE BacklogBatch item, appended to `out` like any other
                # frame — NOT a side-channel slot (this PR's own earlier
                # draft, reverted: side-channelling left `out` empty for a
                # snapshot-only block, so `suspend_between_frames` below
                # never ran for one — see :meth:`_pump_sse`'s own #3570
                # comment for what that yield is for — and an overwrite-
                # before-pop slot is exactly what let a confirmed rapid-
                # switch race silently drop an unpopped batch: the SAME
                # `out`/queue every live frame already goes through has no
                # such slot to overwrite). ``self._backlog_agent``/
                # ``_backlog_sid`` were stamped by the ``session_attached``
                # announce that (by protocol) always precedes this.
                out.append(
                    BacklogBatch(
                        agent=self._backlog_agent,
                        sid=self._backlog_sid,
                        frames=[self._reguard_frame(f) for f in decoded.frames],
                        has_more=decoded.has_more,
                        next_cursor=decoded.next_cursor,
                    )
                )
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
                # ★unconditional clear, conditional re-set (architect
                # non-block, issuecomment-5377689986): the NEXT set() is
                # NOT guaranteed by this client alone — it depends on the
                # SERVER actually re-firing a snapshot, which
                # ``emitter.py:184``'s own guard only does when a
                # ``backlog_provider`` was wired (``if etype ==
                # "session_attached" and self._backlog_provider is not
                # None``). Production always reaches this: ``endpoint.py``'s
                # own AG-UI route always constructs the emitter WITH one
                # (search that file for where it is passed). A caller
                # against a THIRD-PARTY AG-UI server, or a test emitter
                # built without one, would leave this Event cleared
                # forever — ``state_ready()`` never returning is the
                # visible symptom, not a silent wrong answer.
                if (
                    isinstance(decoded, EventFrame)
                    and getattr(decoded.event, "type", None) == "session_attached"
                ):
                    self._state_ready_event.clear()
                    # #5139: this announce always precedes the
                    # MESSAGES_SNAPSHOT re-fire it belongs with (the
                    # reconnect protocol's own ordering) — stamp the
                    # destination NOW so the BacklogBatch built a few
                    # lines below (this same block, next SSE event) can
                    # carry it.
                    edata = getattr(decoded.event, "data", None) or {}
                    self._backlog_agent = str(edata.get("agent", ""))
                    self._backlog_sid = str(edata.get("session_id", ""))
                out.append(self._reguard_frame(decoded))
        return out

    async def _pump_sse(self) -> None:
        """Decode ``self._sse_lines`` and feed each frame into the shared
        display queue — the SAME queue :meth:`put_display` feeds (#5107).

        This is the pre-#5107 body of :meth:`frames` itself, moved
        verbatim into a background task rather than driven inline: a
        locally-authored :meth:`put_display` call must be able to reach
        the renderer WITHOUT waiting on the next SSE line to unblock the
        ``async for`` below (the SSE stream is frequently idle between
        server events — that idle wait is exactly what made the pre-#5107
        no-op invisible rather than merely delayed).

        ALWAYS enqueues :data:`_SSE_DONE` on the way out (``finally``),
        whichever of the 4 exits it takes — the ``__end__`` DisplayFrame
        (normal production shutdown), the source running dry with no
        ``__end__`` (every finite test double), a raised exception
        (connection drop), OR :meth:`close` cancelling this task (a
        ``CancelledError`` still runs ``finally``). Without this,
        :meth:`frames` — which now waits on the shared queue rather than
        driving ``self._sse_lines`` itself — has no way to learn "no more
        SSE frames are coming" and would hang forever on
        ``await self._display_queue.get()``.

        ``put_nowait``, not ``await put`` (architect co-vet,
        issuecomment-5380005757): this queue is unbounded today, so
        ``await put(...)`` never actually suspends — but that is an
        accident of the current ``maxsize``, not a guarantee this method
        can lean on. Under cancellation (the 4th exit above), an ``await``
        inside ``finally`` IS a real suspension point a future bounded
        queue could get cancelled AT, before the sentinel ever lands —
        silently breaking the "ALWAYS" this docstring promises.
        ``put_nowait`` cannot be interrupted mid-call, so the sentinel
        genuinely always lands, regardless of what ``self._display_queue``
        becomes later.

        #5126: a genuine (non-cancellation) exception — the connection dying
        mid-stream — is caught HERE and forwarded through the queue as a
        :class:`_SSEPumpError`, ahead of the ``finally``'s own
        :data:`_SSE_DONE`. Deliberately NOT re-raised after catching: this
        task must exit cleanly (no exception left for asyncio to log as
        "never retrieved") because the queue IS now the retrieval path —
        :meth:`frames` reads the error back off it and raises it in the
        CALLER's context, where a real caller can act on it. Cancellation
        is exempt (bare ``except Exception``, not ``BaseException`` —
        ``asyncio.CancelledError`` derives from ``BaseException`` since
        Python 3.8): :meth:`close`'s own ``.cancel()`` is an intentional,
        expected shutdown, not a failure to report back through the frame
        stream.
        """
        try:
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
                            await self._display_queue.put(frame)
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
                    await self._display_queue.put(frame)
        except Exception as exc:  # noqa: BLE001 -- #5126: forwarded via the
            # queue for `frames()` to re-raise, not re-raised here (see
            # docstring above) -- deliberately broad: ANY failure reading or
            # decoding the SSE stream must reach the caller as a real error,
            # not vanish as an ordinary end-of-stream.
            self._display_queue.put_nowait(_SSEPumpError(exc))
        finally:
            self._display_queue.put_nowait(_SSE_DONE)

    async def frames(self) -> "AsyncIterator[Frame | BacklogBatch]":
        # #5139: widened from ``AsyncIterator[Frame]`` — this transport is
        # the only ``ClientTransport`` implementation that ever yields a
        # ``BacklogBatch`` (see that class's own docstring); every other
        # implementation's ``frames()`` return type is unchanged.
        #
        # Started lazily, once, on first iteration (production calls this
        # exactly once per connection — grepped, #5107 co-vet) rather than
        # in ``__init__``/``start``: an event loop may not be running yet
        # at construction time, and ``asyncio.create_task`` requires one.
        if self._sse_pump_task is None:
            self._sse_pump_task = asyncio.create_task(self._pump_sse())
        while True:
            frame = await self._display_queue.get()
            if isinstance(frame, _SSEPumpError):
                # #5126: the pump died (a real exception, not a clean end or
                # an intentional close()); surface it HERE, in the caller's
                # own context, instead of letting `_SSE_DONE` (queued right
                # after this by the SAME `finally`) make it look like an
                # ordinary end-of-stream.
                #
                # #5694: also flip the tri-state ``has_session``/
                # ``attach_failed`` read — a caller that only catches this
                # exception around its OWN read loop (``_pump_frames``'s
                # #5329 design: log it, keep the app open, do not exit) would
                # otherwise have no way to learn the connection died, and
                # would keep drawing this transport's last-known display AND
                # keep letting the composer POST through the now-orphaned
                # ``send`` closure — the exact silent-misroute shape #5694
                # reports (server-side unsubscribes this connection from
                # ``_CONNECTION_RETARGET_HUB`` when the stream ends, so a
                # later submit falls back to the connect-time URL agent,
                # 200 OK, wrong destination). Setting this here — the one
                # place that already knows the pump is genuinely gone, not
                # merely idle — makes both surfaces degrade from ONE fact.
                self._connected = False
                self._pump_died = True
                raise frame.exc
            if frame is _SSE_DONE:
                return
            # #3570 gate (test_stream_drain_yield_3570.py's own class gate):
            # ``queue.get()`` suspends only while the queue is EMPTY — a
            # burst already sitting in the queue (several ``_pump_sse``
            # frames from one SSE block, or several rapid ``put_display``
            # calls) would otherwise hand every ``yield`` to the consumer
            # with the event loop never running in between. Paired here,
            # not just in ``_pump_sse`` (which no longer yields at all, so
            # its own copy doesn't satisfy this gate) — this is the seam
            # this transport's ``frames()`` actually yields THROUGH now.
            await suspend_between_frames()
            yield frame
            if isinstance(frame, DisplayFrame) and frame.message.kind == "__end__":
                return

    # -- send side ----------------------------------------------------------

    def has_session(self) -> bool:
        return self._connected

    def attach_failed(self) -> bool:
        # #5096 review finding (lead-coder): EXPLICITLY implemented, not
        # inherited from ClientTransportStub. A remote attach either
        # already succeeded by the time --connect returns or the
        # connection attempt itself raised, so there was originally no
        # separate "connecting in the background" phase to fail remotely
        # here (see ClientTransport.attach_failed's own docstring for the
        # full rationale — only InProcessTransport's background-attach
        # path had anything meaningful to report).
        #
        # #5694: that is no longer the whole story — an ALREADY-attached
        # connection's own SSE pump can still die mid-session (a genuine
        # read failure, ``_pump_died`` above). ``has_session()`` alone
        # cannot distinguish that from "still connecting" (both read
        # false), and the tri-state this pair feeds
        # (``TextualChatApp._attach_state``: "connecting" | "failed" |
        # None) must not call a DEAD connection "connecting" — this app
        # never auto-reconnects, so telling the operator to keep waiting
        # would be the "still loading" paper-over that tri-state's own
        # owner ruling forbids. Reusing ``attach_failed`` (rather than a
        # third, parallel flag) means the header AND the composer's
        # submit-gate (#3671 P3, ``on_composer_submitted``) — which
        # already both read this exact pair — degrade together, with no
        # new call site to keep in sync.
        return self._pump_died

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

    async def request_session_list(self) -> "list[dict]":
        # #5099: POST a typed request; the server reads its OWN copy of
        # the registry (scoped to THIS connection's own agent_name, baked
        # into the endpoint URL at connect time — same reasoning
        # ``request_artifact_list`` gives for reading the server's live
        # copy rather than trusting a stale wire view) and answers with
        # the current session list.
        result = await self._send({"type": "session_list_request"})
        sessions = (result or {}).get("sessions")
        return sessions if isinstance(sessions, list) else []

    async def request_older_backlog(self, before_root_id: str) -> None:
        """#5139 C: client-driven pull for the NEXT-older backlog page —
        ``on_flow_view_reached_top``'s remote counterpart to local's
        on-disk ``_extend_older_frames_from_disk``. POSTs a typed request
        (mirrors ``request_session_list`` etc. above); the response reuses
        the EXACT ``MESSAGES_SNAPSHOT`` wire shape a live reconnect/switch
        already decodes (:func:`~reyn.interfaces.transport.agui.protocol.decode_event`),
        so this reuses that same decode path rather than a second one.

        The decoded page is wrapped as a :class:`BacklogBatch` with
        ``is_older_page=True`` (PREPEND, not append —
        ``TextualChatApp._apply_backlog_batch``'s own branch on that flag)
        and fed into the SAME queue every other frame this transport
        produces flows through (:meth:`put_display`'s own established
        pattern) — a new PRODUCER onto the existing delivery path, not a
        new one. A failed/empty response is silently dropped (never a
        fabricated empty page — the caller's own ``has_more``/cursor state
        is simply left as it was, so a transient failure retries on the
        next ``ReachedTop`` rather than being mistaken for "reached the
        true start")."""
        result = await self._send(
            {
                "type": "load_older_backlog_request",
                "session_id": self._backlog_sid,
                "before_root_id": before_root_id,
            },
        )
        if not result:
            return
        decoded = decode_event(MESSAGES_SNAPSHOT, result)
        if not isinstance(decoded, MessagesSnapshot):
            return
        self._display_queue.put_nowait(
            BacklogBatch(
                agent=self._backlog_agent,
                sid=self._backlog_sid,
                frames=[self._reguard_frame(f) for f in decoded.frames],
                has_more=decoded.has_more,
                next_cursor=decoded.next_cursor,
                is_older_page=True,
            )
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
        # #5107 (architect ruling B, issuecomment-5379950484; lead-coder's
        # contract-first correction, issuecomment-5379955824): the contract
        # (ClientTransport.put_display's own docstring) only ever asked for
        # "show it on this client's own face" -- this transport CAN satisfy
        # that; a remote client genuinely cannot inject into the SERVER's
        # own outbox (a different, stronger claim the old docstring here
        # made, which was never this method's job to satisfy). Feeds the
        # SAME queue the SSE-sourced frames merge into (self._pump_sse) so
        # a slash reply / echo / unknown-command note renders through the
        # identical renderer path a server-sent message does.
        self._display_queue.put_nowait(DisplayFrame(msg))

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

    async def clear_pending_command_ui(self) -> None:
        # #5096 review finding (lead-coder): EXPLICITLY implemented, not
        # inherited from ClientTransportStub, even though the VALUE
        # matches its own default (a no-op) — command-UI is INLINE-APP-
        # LOCAL state, never on the wire (mirrors pending_command_ui()'s
        # own None for remote — see ClientTransport.clear_pending_
        # command_ui's own docstring). #5048's cutover leans on this
        # remaining a deliberate no-op here, not a forgotten override.
        return None

    def reyn_state_root(self) -> "Path | None":
        # #5096 review finding (lead-coder): EXPLICITLY implemented, not
        # inherited from ClientTransportStub. The project lives on the
        # far end of the wire — there is no local answer to give, ever,
        # not a transient failure (see ClientTransport.reyn_state_root's
        # own docstring for the None-vs-empty distinction this preserves).
        return None

    async def shutdown(self) -> None:
        # Client-LOCAL disconnect only (A3). A client can never tear down the
        # single-writer server — no shutdown is sent over the wire (closing the
        # ws footgun where a client kills the server). Just drop the connection.
        self._connected = False


__all__ = ["AgUiTransport"]
