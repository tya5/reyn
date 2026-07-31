"""#3570 (1/2) — input arriving DURING a stream must be handled in bounded time.

``InProcessTransport.frames`` awaits ``asyncio.Queue.get()`` once per frame,
which reads like a suspension point per frame but is not one: ``get()`` returns
WITHOUT suspending whenever the queue is non-empty. A provider that delivers
several deltas per socket read — litellm's stream wrapper yields every chunk a
single read carried, and awaiting a coroutine that does not suspend is not a
scheduling point — therefore leaves the consumer draining to exhaustion with
ZERO returns to the event loop. Nothing else runs for the whole burst: not the
animation, not a keystroke, not a timer. That is the owner's report ("the UI is
frozen for the whole stream and comes back the instant it ends").

**The contract this file gates**: input injected while a stream is arriving is
handled within a BOUNDED number of frames — bounded by construction (one
suspension per frame) rather than by how full the queue happens to be, which is
a timing property.

The bound is expressed in FRAMES, never in wall-clock: a millisecond deadline
would be a load-sensitive gate that fails only under ``-n auto`` concurrency,
which is the class #3473 just closed. The delta the app is ingesting is the
logical tick.

Real ``AgentRegistry`` + real ``Session`` + real ``InProcessTransport`` + real
``TextualChatApp``; the deltas ride the production chat-event emit
(``host.events.emit("agent_delta", ...)``, the call ``RouterLoop
._emit_agent_delta`` makes) and the tool frames ride ``repl_outbox`` (the
display path the registry forwarder feeds). No mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.message import Message

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame, FrameTag
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from tests._support.agent_session import make_session

#: One delta's worth of text. Small, like a real token chunk.
_CHUNK = "lorem ipsum "


class _InputProbe(Message):
    """A message posted onto the app's own queue — the SAME ``App`` message pump
    a keystroke is delivered on, so handling it means the app got the processor
    back, not that some surface merely looked alive (a spinner animates happily
    on stale state, which is why "it looked smooth" is never the witness here)."""


class _InputProbeApp(TextualChatApp):
    """The REAL app with two observations added: which delta the pump is on, and
    at which delta a probe posted mid-backlog is handled.

    A subclass recording around ``super()`` calls, never a stand-in — the idiom
    ``tests/test_transport_bit_identical.py``'s ``_RecordingInlineRenderer``
    uses. Both readings are counted in FRAMES (the logical tick), which is what
    keeps the gate off wall-clock and therefore stable under load.
    """

    inject_after_deltas = 20

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.deltas_ingested = 0
        self.probe_injected_at: "int | None" = None
        self.probe_handled_at: "int | None" = None

    def _handle_agent_delta_event(self, event) -> None:
        self.deltas_ingested += 1
        if (
            self.probe_injected_at is None
            and self.deltas_ingested >= self.inject_after_deltas
        ):
            self.probe_injected_at = self.deltas_ingested
            self.post_message(_InputProbe())
        super()._handle_agent_delta_event(event)

    def on__input_probe(self, message: _InputProbe) -> None:
        if self.probe_handled_at is None:
            self.probe_handled_at = self.deltas_ingested


def _tool_started(op_id: str) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started",
        text="grep",
        meta={"tool": "grep", "op_id": op_id, "args": {}},
    )


def _tool_completed(op_id: str) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="grep",
        meta={"tool": "grep", "op_id": op_id, "result": "ok"},
    )


async def _build(tmp_path: Path, app_cls=TextualChatApp):
    """Real registry + session + transport + app, wired as ``reyn chat`` wires
    them (``repl.py``: build the transport over the registry, ``start`` it, hand
    it to the app)."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            registry=holder.get("reg"),
            snapshot_path=tmp_path / "snapshot.json",
            workspace_base_dir=tmp_path / "ws",
            workspace_state_dir=tmp_path / "state",
        )

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=factory, state_log=state_log
    )
    holder["reg"] = registry
    if not registry.exists("default"):
        registry.create("default")
    await registry.attach("default")
    transport = InProcessTransport(
        registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID
    )
    transport.start()
    return registry, transport, app_cls(transport=transport)


async def _until(pred, *, attempts: int = 400, delay: float = 0.01) -> bool:
    """Bounded poll — a hang exhausts the budget and returns False (RED). This is
    the test's own patience, not the property under test (which is counted in
    frames)."""
    for _ in range(attempts):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


@pytest.mark.asyncio
async def test_input_arriving_during_a_stream_is_handled_within_a_few_frames(
    tmp_path,
) -> None:
    """Tier 2: ★★the contract — input injected WHILE a backlog of stream frames
    is being drained is handled within a bounded number of frames, not after the
    whole backlog.

    The backlog is MIXED — two chain_ids interleaved, with tool frames landing
    between them on the SAME ordered queue — because a clean single-chain run is
    the easy case and would not catch a suspension point that another frame kind
    can defeat.

    Strip-falsify (recorded in the PR body): removing the ``await
    asyncio.sleep(0)`` from ``InProcessTransport.frames`` pushes the probe's
    handling to the END of the backlog — the frozen UI, reproduced.
    """
    per_chain = 150
    arrivals = per_chain * 2
    registry, transport, app = await _build(tmp_path, app_cls=_InputProbeApp)
    session = registry.attached_session()
    try:
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            for i in range(per_chain):
                for chain in ("chain-a", "chain-b"):
                    session.router_host.events.emit(
                        "agent_delta", text=_CHUNK, chain_id=chain
                    )
                if i % 25 == 0:
                    registry.repl_outbox.put_nowait(_tool_started(f"op-{i}"))
                    registry.repl_outbox.put_nowait(_tool_completed(f"op-{i}"))

            assert await _until(
                lambda: app.deltas_ingested >= arrivals
            ), "the mixed backlog never drained"
            assert app.probe_handled_at is not None, (
                "the injected input was never handled at all"
            )
            waited = app.probe_handled_at - app.probe_injected_at
            assert waited < 0, (
                f"input injected at frame {app.probe_injected_at} was only handled "
                f"{waited} frames later, out of a {arrivals}-frame backlog — the "
                "drain loop holds the processor for the whole stream"
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_the_stream_still_terminates_at_the_end_frame(tmp_path) -> None:
    """Tier 1: the added suspension point does not change WHEN the frame stream
    ends: ``__end__`` is delivered, and nothing queued behind it is.

    The suspension is one yield per frame — there is no batching, so there is no
    "``__end__`` arrived mid-batch" case to decide. This gate pins that: the
    terminal check still runs on the SAME frame it always did, immediately after
    that frame is yielded, whether or not more frames are already queued behind
    it (they are here — the queue is pre-filled past the end, which is exactly
    the non-empty regime that made ``get()`` stop suspending in the first place).
    """
    registry, transport, _app = await _build(tmp_path)
    session = registry.attached_session()
    try:
        session.router_host.events.emit("agent_delta", text="a", chain_id="c")
        registry.repl_outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        registry.repl_outbox.put_nowait(OutboxMessage(kind="status", text="after"))
        await asyncio.sleep(0)  # let the outbox pump re-tag onto the frame stream

        seen = []
        async for frame in transport.frames():
            seen.append(frame)

        kinds = [
            f.message.kind if f.tag is FrameTag.DISPLAY else "<event>" for f in seen
        ]
        assert "__end__" in kinds, f"the terminal frame was never delivered: {kinds}"
        assert kinds[-1] == "__end__", (
            f"frames were delivered after the terminal frame: {kinds}"
        )
        assert isinstance(seen[0], (DisplayFrame, EventFrame))
    finally:
        await registry.shutdown()
