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
``TextualChatApp``; the deltas ride the production audit-event emit
(``host.events.emit("agent_delta", ...)``, the call ``RouterLoop
._emit_agent_delta`` makes) and the tool frames ride ``repl_outbox`` (the
display path the registry forwarder feeds). No mocks.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from textual.message import Message

import reyn.interfaces.transport as transport_pkg
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource
from reyn.interfaces.transport.agui.protocol import encode_frame, to_sse
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
    ``tests/interfaces/test_transport_bit_identical.py``'s ``_RecordingInlineRenderer``
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


async def _until(pred, *, delay: float = 0.01) -> None:
    """Poll for ``pred`` (owner policy, #3748 -- unbounded).

    Not a proxy for how long a 300-frame backlog takes to drain under
    ``-n auto`` (irrelevant to the PROPERTY, which is counted in frames,
    not wall time). A genuine hang is CI's own ``--timeout=120`` kill,
    which surfaces via the kill stack showing this exact loop -- a
    second, test-local budget would just be a second kill-switch racing
    the real one.
    """
    while not pred():
        await asyncio.sleep(delay)


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

            await _until(lambda: app.deltas_ingested >= arrivals)
            assert app.probe_handled_at is not None, (
                "the injected input was never handled at all"
            )
            waited = app.probe_handled_at - app.probe_injected_at
            assert waited < arrivals // 10, (
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


async def _suspension_gaps_while_draining(frames, *, count: int) -> "list[int]":
    """Drain ``count`` frames while a co-running task counts its own turns, and
    return the counter's advance BETWEEN consecutive frames.

    A gap of 0 means the loop never came back between those two frames — the
    starvation this issue is about. Sampling happens INSIDE the drain, so no
    ``sleep`` in a test body can inflate the reading (a co-task left spinning
    across a polling wait would report health the drain never had)."""
    counter = [0]
    stop = asyncio.Event()

    async def co_task() -> None:
        while not stop.is_set():
            counter[0] += 1
            await asyncio.sleep(0)

    runner = asyncio.create_task(co_task())
    samples: "list[int]" = []
    try:
        async for _frame in frames:
            samples.append(counter[0])
            if len(samples) >= count:
                break
    finally:
        stop.set()
        await runner
    return [b - a for a, b in zip(samples, samples[1:])]


@pytest.mark.asyncio
async def test_the_agui_client_drain_suspends_between_frames_of_one_read(
    tmp_path,
) -> None:
    """Tier 2: ★the SECOND ``ClientTransport`` — ``AgUiTransport`` — returns to
    the loop between frames of a buffered SSE read, not only between reads.

    Its starvation has a different source from ``InProcessTransport``'s: no
    queue is involved. ``async for raw in self._sse_lines`` awaits a line
    iterator that, over an already-buffered read, produces every line without
    suspending, and one decoded block can carry MANY frames through an inner
    ``for`` loop with no await of its own (a MESSAGES_SNAPSHOT reconnect
    carries the whole backlog that way). Enumerating the three implementations
    and patching each was the easy half; this is the half that says the patch
    is load-bearing HERE and not just in the site that happened to have a test.

    Real wire bytes (the production ``encode_frame`` + ``to_sse``) through the
    real ``AgUiTransport``.

    Strip-falsify (PR body): dropping the suspension from
    ``AgUiTransport.frames`` makes every gap 0 — the whole SSE buffer is
    decoded and delivered without the loop running anything else."""
    count = 60
    sse = "".join(
        to_sse(encode_frame(DisplayFrame(OutboxMessage(kind="agent", text=f"r{i}"))))
        for i in range(count)
    )

    async def _lines():
        for line in sse.split("\n"):
            yield line

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_lines(), _noop_send)
    gaps = await _suspension_gaps_while_draining(transport.frames(), count=count)

    starved = [g for g in gaps if g == 0]
    assert gaps, "test setup: no frames were decoded from the SSE buffer"
    assert len(starved) < len(gaps) // 10, (
        f"{len(starved)} of {len(gaps)} consecutive frames were delivered with "
        "the event loop running nothing in between — the AG-UI client drain "
        "starves the loop for a whole buffered read"
    )


@pytest.mark.asyncio
async def test_the_server_frame_source_suspends_between_frames_of_a_burst(
    tmp_path,
) -> None:
    """Tier 2: ★the THIRD site — the AG-UI endpoint's per-connection frame
    source — returns to the loop between frames too.

    Not a ``ClientTransport`` (it is the server's own fan-out), but the same
    ``asyncio.Queue`` + synchronous ``put_nowait`` shape, and its consumer
    encodes and serializes every frame onto a socket. Starving there holds the
    SERVER's loop: other connections' writes and the fail-close driver's timers
    stop for the burst, which is a strictly wider blast radius than one TUI.

    Real ``Session`` + real ``_SessionFrameSource``; the burst is emitted
    through the production audit-event channel, so the queue fills exactly the
    way a streamed reply fills it.

    Strip-falsify (PR body): dropping the suspension makes every gap 0."""
    count = 60
    registry, _transport, _app = await _build(tmp_path)
    session = registry.attached_session()
    source = _SessionFrameSource(session, registry=registry, agent_name="default")
    source.start()
    try:
        for i in range(count + 5):
            # #5259: a per-connection drain now collapses a run of CONSECUTIVE
            # same-chain deltas into one frame, so a same-chain burst no longer
            # produces a burst of FRAMES — there would be nothing to measure a
            # gap between. Alternating the chain keeps every delta its own
            # frame, which is the shape this test is about: the queue is full
            # and ``get()`` stops suspending, so the drain must return to the
            # loop itself. (For the same-chain case #5259 removes the starvation
            # rather than mitigating it: one frame, one encode.)
            session.router_host.events.emit(
                "agent_delta", text=f"d{i}", chain_id=f"chain{i % 2}"
            )
        gaps = await _suspension_gaps_while_draining(source.frames(), count=count)

        starved = [g for g in gaps if g == 0]
        assert gaps, "test setup: the burst never reached the source's queue"
        assert len(starved) < len(gaps) // 10, (
            f"{len(starved)} of {len(gaps)} consecutive frames were delivered "
            "with the event loop running nothing in between — the server-side "
            "drain starves the server's loop for the whole burst"
        )
    finally:
        source.close()
        await registry.shutdown()


def test_every_frame_drain_in_the_transport_package_pairs_yields_with_a_suspension() -> None:
    """Tier 2: ★the CLASS gate — every ``frames()`` drain in the transport
    package pairs each ``yield`` with a call to
    :func:`~reyn.interfaces.transport.drain.suspend_between_frames`.

    Three sites fixed by hand is a fact about today; a FOURTH transport is
    where the defect gets reimported, by an author who never read this issue.
    The enumeration is over the PACKAGE's source (every ``async def frames``
    under ``reyn/interfaces/transport``), not over ``ClientTransport``
    subclasses, because the server-side source is one of the three sites and is
    not a subclass — a subclass-only enumeration would have declared complete
    coverage while missing it.

    ★What this gate can and cannot see: it is a pairing count (a suspension per
    ``yield``), not a path analysis — it catches "a yield was added without a
    suspension point", which is the reimport shape, and it cannot prove a
    suspension is reached on every branch. The behavioural gates above are what
    say the suspension actually fires; this one says a new site cannot quietly
    skip having one.

    Non-vacuous: the scan must find the three known drains by name, so a
    broken scanner reports its own breakage instead of passing empty."""
    package = Path(transport_pkg.__file__).parent
    found: "dict[str, tuple[int, int]]" = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name != "frames":
                continue
            yields = sum(1 for n in ast.walk(node) if isinstance(n, ast.Yield))
            suspensions = sum(
                1
                for n in ast.walk(node)
                if isinstance(n, ast.Await)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", getattr(n.value.func, "attr", None))
                == "suspend_between_frames"
            )
            found[f"{path.relative_to(package)}::{node.name}"] = (yields, suspensions)

    known = {"in_process.py::frames", "agui/client.py::frames", "agui/endpoint.py::frames"}
    assert known <= set(found), (
        f"the drain scan did not see {sorted(known - set(found))} — it found "
        f"{sorted(found)}. The scanner is broken (a moved/renamed drain, a parse "
        "failure), so its silence about the rest means nothing"
    )
    unpaired = {
        site: counts for site, counts in found.items() if counts[1] < counts[0]
    }
    assert not unpaired, (
        f"frame drain(s) yielding without a paired suspension point: {unpaired} "
        "(site -> (yields, suspensions)). A drain that yields more often than it "
        "suspends can hand a burst to its consumer with the event loop never "
        "running in between — see reyn.interfaces.transport.drain"
    )
