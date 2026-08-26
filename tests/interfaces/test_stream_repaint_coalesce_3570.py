"""#3570 — a streamed reply must not spend the event loop once per arriving delta.

Two independent defects on the same path, each with its own gate here, because
neither one's fix produces the other's property (measured, PR body has the table):

1. **The frame drain loop had no unconditional yield point.**
   ``InProcessTransport.frames`` awaits ``asyncio.Queue.get()`` per frame, but
   ``get()`` returns WITHOUT suspending while the queue is non-empty — so a
   provider that delivers several deltas per read (measured occupancy 5..2000
   on the real path) is drained to exhaustion with ZERO returns to the loop.
   Whether the UI breathes was a function of queue occupancy — a timing
   property — rather than of the loop's structure.

2. **Every visible delta bought its own repaint.** ``_flush_streaming_reply``
   issued a ``set_item`` per delta, i.e. a present + a strip render of the WHOLE
   accumulated body at the provider's rate rather than the terminal's.

The gates below are written as PROPERTIES, never as counts: an exact number of
scheduler passes or repaints is a function of machine speed and would flake.
What is asserted is "another task got the loop while the burst drained" and
"repaints stopped tracking arrivals, while not one byte of text was dropped".

Real ``AgentRegistry`` + real ``Session`` + real ``RouterLoop`` + real
``InProcessTransport`` + real ``TextualChatApp`` throughout — the deltas are
emitted by the production ``on_content_delta`` wiring
(``RouterLoop._emit_agent_delta`` → session audit-events → the registry's focus
listener → the transport queue → the app's frame pump). The LLM call itself is
the only faked boundary, the established idiom of
``tests/interfaces/test_agent_delta_audit_event_3288.py`` and
``tests/interfaces/test_3327_answer_bypasses_sentqueue.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from textual_flowview import FlowView

from reyn.core.events.events import Event
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _STREAM_REPAINT_MIN_INTERVAL
from reyn.interfaces.transport.frames import EventFrame
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from tests._support.agent_session import make_session

_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)

#: One delta's worth of text. Small, like a real token chunk.
_CHUNK = "lorem ipsum "


class _DeltaCountingApp(TextualChatApp):
    """The REAL app with one observation added: how many deltas the pump has
    ingested. A subclass recording around a ``super()`` call, never a stand-in
    (the ``_RecordingInlineRenderer`` idiom).

    It exists so the gates can wait on the app's OWN progress through the
    backlog instead of waiting for a repaint to land, which would make them
    depend on a timer under whatever load the run happens to have."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.deltas_ingested = 0

    def _handle_agent_delta_event(self, event) -> None:
        self.deltas_ingested += 1
        super()._handle_agent_delta_event(event)


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


class _Clock:
    """The app's OWN injection point (``TextualChatApp(clock=...)``), driven
    instead of slept through — the idiom ``tests/interfaces/test_stream_spinner_3530.py``
    already uses for the blink. Not a stand-in for a collaborator: the app takes
    a ``Callable[[], float]`` by design and production passes ``time.monotonic``.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _build(tmp_path: Path, clock=None, app_cls=TextualChatApp):
    """Real registry + session + transport + app, wired exactly as ``reyn chat``
    wires them (``repl.py``: construct the transport over the registry, ``start``
    it, then hand it to the app)."""
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
    app = (
        app_cls(transport=transport, clock=clock)
        if clock
        else app_cls(transport=transport)
    )
    return registry, transport, app


def _install_bursting_llm(monkeypatch, *, chunks: int) -> None:
    """Patch the LLM boundary with a real async callable that fires
    ``on_content_delta`` ``chunks`` times WITHOUT awaiting in between.

    That is not an artificial shape: litellm's stream wrapper yields every chunk
    a single socket read carried, and awaiting a coroutine that returns without
    suspending is not a scheduling point — which is exactly how the frame queue
    goes non-empty in production (occupancy 2000 measured for this shape).
    """

    async def _fake_llm(*_args, **kwargs):
        on_delta = kwargs.get("on_content_delta")
        assert on_delta is not None, (
            "RouterLoop must thread on_content_delta into call_llm_tools for this "
            "to be a real streaming-wiring witness"
        )
        for _ in range(chunks):
            now = datetime.now().astimezone()
            on_delta(_CHUNK, raw_chunk_count=1, first_arrival=now, last_arrival=now)
        return LLMToolCallResult(
            content=_CHUNK * chunks,
            tool_calls=[],
            finish_reason="stop",
            usage=_USAGE,
        )

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _fake_llm)


def _entry_for(app: TextualChatApp, chain_id: str):
    """The flow entry a given reply is coalescing into, via PUBLIC
    ``FlowView.entries`` and the entry's own ``chain_id`` meta."""
    for entry in app.query_one(FlowView).entries:
        if (entry.item.meta or {}).get("chain_id") == chain_id:
            return entry
    return None


def _streamed_entry(app: TextualChatApp):
    """The flow entry the streamed reply is coalescing into, or ``None``."""
    for entry in app.query_one(FlowView).entries:
        if getattr(entry.item, "kind", None) == "agent":
            return entry
    return None


async def _until(pred, *, delay: float = 0.01) -> None:
    """Poll for ``pred`` (owner policy, #3748 -- unbounded). A hang surfaces
    via CI's own kill (the kill stack shows this exact loop), not a
    test-local budget racing it."""
    while not pred():
        await asyncio.sleep(delay)


@pytest.mark.asyncio
async def test_repaints_stop_tracking_arrivals_without_dropping_text(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: ★the number of repaints follows the repaint budget, not the
    number of deltas — and the accumulated text is complete regardless.

    ``Entry.revision`` is flowview's public per-``set_item`` counter (it is the
    presentation cache key), so it is the repaint count read from the public
    surface rather than from app internals.

    Both halves matter and neither implies the other: dropping repaints is only
    admissible because the text is accumulated unconditionally, so the gate
    asserts the FULL body arrives (through the terminal completion frame, which
    is authoritative) in the same run that asserts the repaints were coalesced.

    Strip-falsify (PR body): making ``_repaint_streaming_reply_within_budget``
    flush unconditionally puts the revision back in step with the delta count
    and this gate goes RED, while the text half stays green — the two halves
    fail independently.

    ★ #3748 experiment fix: same real-timer dependency #3746 found in this
    file's mixed-backlog sibling. With ``clock`` frozen, the budget-check
    path never fires, so every repaint the old ``< chunks // 10`` threshold
    measured came from the catch-up timer's real ``set_timer`` (real
    wall-clock, not the injected clock) — caught live by the #3748
    ``attempts=1`` experiment (21 repaints on one run, one over the old
    threshold of 20). Neutralized here too, for the same reason: the
    catch-up timer's own real-time behavior is already dedicated-tested by
    ``test_a_deferred_repaint_is_painted_within_the_budget_window``. Unlike
    the mixed-backlog sibling, the expected count with it disabled is
    exactly 1, not 0 — the terminal completion frame's own
    ``entry.set_item`` (below, in ``_ingest_frame``) is a real, unconditional
    repaint independent of the catch-up timer, and this test's OWN scenario
    runs to completion (unlike the mixed-backlog test, which asserts
    mid-stream, before any completion frame arrives).
    """
    chunks = 200
    clock = _Clock()  # frozen: no delta can ever be "due" on the budget clock
    _install_bursting_llm(monkeypatch, chunks=chunks)
    registry, transport, app = await _build(tmp_path, clock=clock)
    # #3748: see the docstring above — neutralize the real-wall-clock catch-up
    # timer so the measured revision count is fully deterministic. Safe per
    # the method's own contract (an optimisation only) and covered in
    # isolation by the sibling test named above.
    monkeypatch.setattr(app, "_schedule_streaming_catchup", lambda: None)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.submit_user_text("stream please")
            await _until(lambda: _streamed_entry(app) is not None)
            entry = _streamed_entry(app)
            await _until(lambda: _CHUNK * chunks in str(entry.item.text))

            # With the catch-up timer disabled and the budget clock frozen, the
            # ONLY possible repaint is the terminal completion frame's own
            # unconditional `entry.set_item` — exactly 1, deterministically.
            assert entry.revision == 1, (
                f"{entry.revision} repaints for {chunks} deltas — expected "
                "exactly 1 (the terminal completion write); the repaint "
                "budget is not coalescing"
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_a_mixed_backlog_coalesces_per_chain_not_per_delta(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ★repaints follow the number of REPLIES in flight, not the number
    of deltas — on a backlog that is *mixed*, not a clean run of one chain.

    A pure single-chain burst is the easy case: it cannot tell a working
    coalescer from one that is silently reset by anything else on the queue. The
    backlog here interleaves two chain_ids (``_streaming_replies`` is keyed by
    chain_id, so each must accumulate independently) with tool frames landing
    between them on the SAME ordered queue.

    ★ It is also the gate on a design decision: a non-delta frame does NOT flush
    pending repaints. Flushing on tool frames would look like "keep the surface
    in order", but an entry's POSITION is fixed at append time, so updating a
    row above later cannot reorder anything; a tool frame touches a different
    entry entirely; and the terminal completion frame settles its own reply with
    the authoritative text (``_ingest_frame``) rather than needing a flush
    first. Flushing on them would only make the coalescing decay in proportion
    to how much else the turn emits — which is exactly the tool-heavy turn where
    the loop is busiest.

    Real transport, real ordered queue: deltas ride the production audit-event
    emit (``host.events.emit("agent_delta", ...)``, the SAME call
    ``RouterLoop._emit_agent_delta`` makes) and the tool frames ride
    ``repl_outbox`` (the display path the registry forwarder feeds).

    ★ #3746 fix: ``_schedule_streaming_catchup`` is neutralized here. With
    ``clock`` frozen, ``_repaint_streaming_reply_within_budget``'s own
    comparison (``self._clock() - record.last_repaint``) never crosses the
    budget, so the DIRECT flush path never fires during this loop — every
    repaint the old assertion measured came exclusively from the catch-up
    TIMER, which is armed via Textual's real ``set_timer`` (real wall-clock,
    NOT the injected clock — confirmed by reading ``message_pump.py``). The
    "frozen: no repaint can be due" comment above was true for the budget
    check but not for this second path, so the repaint count this test
    measured was actually "how many real 33ms windows elapsed while
    processing 200 arrivals on this machine, under this load" — environment-
    and load-dependent by construction, not a stable per-arrival ratio.
    Measured directly: 2-4 repaints across 15 local runs (Python 3.12), a
    CI run hit exactly 20 (the `arrivals // 10` threshold this test used to
    assert against) once. The catch-up timer's own real-time behavior is
    already dedicated-tested by
    ``test_a_deferred_repaint_is_painted_within_the_budget_window`` above,
    so this test does not need to also exercise it — disabling it here
    makes THIS test's own claim (chain interleaving + tool frames do not
    defeat per-chain coalescing) assert on the fully deterministic path
    instead of racing a real timer against however long a real backlog
    drain happens to take.
    """
    chains = ("chain-a", "chain-b")
    per_chain = 100
    arrivals = per_chain * len(chains)
    clock = _Clock()  # frozen: no repaint can be "due" on the budget clock
    registry, transport, app = await _build(
        tmp_path, clock=clock, app_cls=_DeltaCountingApp
    )
    # #3746: see the docstring above — the catch-up timer is real-wall-clock
    # driven regardless of the frozen `clock`, so it is the ONLY source of
    # non-determinism left once the budget check itself never fires. A no-op
    # here is safe per the method's own contract ("the budget is an
    # OPTIMISATION... failing to arm the timer must degrade... never break
    # the pump") and covered in isolation by the sibling test above.
    monkeypatch.setattr(app, "_schedule_streaming_catchup", lambda: None)
    session = registry.attached_session()
    try:
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            for i in range(per_chain):
                for chain in chains:
                    session.router_host.events.emit(
                        "agent_delta", text=_CHUNK, chain_id=chain
                    )
                if i % 10 == 0:  # tool frames interleaved on the same queue
                    registry.repl_outbox.put_nowait(_tool_started(f"op-{i}"))
                    registry.repl_outbox.put_nowait(_tool_completed(f"op-{i}"))

            # Wait on INGEST, never on a repaint landing: the ingest count is the
            # app's own progress through the backlog, whereas waiting for paint
            # would make the gate depend on a timer under whatever load the run
            # happens to have.
            await _until(lambda: app.deltas_ingested >= arrivals)
            entries = {c: _entry_for(app, c) for c in chains}
            assert all(e is not None for e in entries.values())

            # Every reply is on screen from its first delta (the append carries
            # it, which is why the count below starts from a painted row, not a
            # blank one); ``revision`` then counts only the RE-paints. With the
            # catch-up timer neutralized and the budget clock frozen, NO repaint
            # can happen during this loop at all — asserting exactly 0 is the
            # deterministic, environment-independent form of the same claim the
            # old approximate `< arrivals // 10` threshold was reaching for.
            assert all(_CHUNK in str(e.item.text) for e in entries.values())
            repaints = sum(e.revision for e in entries.values())
            assert repaints == 0, (
                f"{repaints} repaints for {arrivals} deltas across {len(chains)} "
                "replies with the catch-up timer disabled and the budget clock "
                "frozen — no repaint should be possible at all; the direct "
                "budget-check path fired when it should not have"
            )

            # ...and nothing was dropped: one more delta per chain, past the
            # budget window, flushes each reply's whole accumulation. The
            # repaint budget may skip a render, never an append — a hang
            # below means the opposite happened (a coalesced reply lost
            # text); the per-entry counts that used to print on failure no
            # longer do (#3748: a hang surfaces via CI's kill, not a
            # message).
            clock.advance(_STREAM_REPAINT_MIN_INTERVAL * 2)
            for chain in chains:
                session.router_host.events.emit(
                    "agent_delta", text=_CHUNK, chain_id=chain
                )
            await _until(
                lambda: all(
                    str(e.item.text).count(_CHUNK) == per_chain + 1
                    for e in entries.values()
                )
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_a_pending_repaint_never_survives_the_session_switch_barrier(
    tmp_path,
) -> None:
    """Tier 2: ★text held back by the repaint budget when a ``session_attached``
    barrier arrives is DISCARDED, never written to the screen after the reset.

    The decision, stated: a deferred repaint is **dropped** at the barrier, not
    flushed before it. #3310 N2 makes ``session_attached`` the point where every
    per-session client state resets and the flow is rehydrated from the NEW
    session; painting the old session's accumulation after that point would put
    a previous conversation's text on the new one's screen. That is not a text
    LOSS — the old session's own history holds it — it is contamination, which
    is the worse failure, so the barrier wins over the pending render.

    The mechanism is the barrier handler clearing ``_streaming_replies``: the
    catch-up timer reads that dict when it fires, so a record dropped at the
    barrier can no longer be flushed by anything.

    Non-vacuous by construction, in two legs of the SAME run: leg 1 shows a
    deferred delta of a reply with no barrier behind it DOES reach the screen
    (so "absent" in leg 2 is the barrier's doing, not a stream that never
    delivered); leg 2 queues the barrier in the SAME synchronous block as the
    deferred delta — no await in between, so no timer can have flushed it — and
    the text must never appear afterwards.
    """
    clock = _Clock()  # frozen: every delta after the first is deferred
    registry, transport, app = await _build(tmp_path, clock=clock)
    session = registry.attached_session()
    kept = "KEPT-DEFERRED-TEXT"
    dropped = "OLD-SESSION-TEXT"
    try:
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            # Leg 1 (control): a deferred delta with no barrier behind it is
            # painted by the catch-up.
            session.router_host.events.emit(
                "agent_delta", text=_CHUNK, chain_id="kept-chain"
            )
            session.router_host.events.emit(
                "agent_delta", text=kept, chain_id="kept-chain"
            )
            # control leg: a deferred repaint must reach the screen on its
            # own (no barrier follows it here) — leg 2 below is the barrier
            # case this is the baseline for.
            await _until(
                lambda: (
                    _entry_for(app, "kept-chain") is not None
                    and kept in str(_entry_for(app, "kept-chain").item.text)
                )
            )

            # Leg 2: the barrier is queued in the same synchronous block as the
            # deferred delta — nothing could have flushed it in between.
            session.router_host.events.emit(
                "agent_delta", text=_CHUNK, chain_id="old-chain"
            )
            session.router_host.events.emit(
                "agent_delta", text=dropped, chain_id="old-chain"
            )
            registry.repl_outbox.put_nowait(
                EventFrame(
                    Event(
                        type="session_attached",
                        data={"agent": "default", "session_id": "other"},
                    )
                )
            )
            await _until(lambda: _entry_for(app, "kept-chain") is None)

            # Well past any armed catch-up window.
            for _ in range(20):
                await pilot.pause()
            texts = [str(e.item.text) for e in app.query_one(FlowView).entries]
            assert not any(dropped in t for t in texts), (
                "a repaint deferred before the session-switch barrier wrote the "
                f"OLD session's text onto the NEW session's screen: {texts}"
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_a_deferred_repaint_is_painted_within_the_budget_window(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: ★the deferral is BOUNDED — text held back by the repaint budget
    reaches the screen on its own, with no further delta and no completion.

    This is the hazard the budget introduces and must close: a producer fast
    enough that the frame queue never empties, or a model that pauses mid-reply,
    must still paint progressively rather than showing nothing until the reply
    completes. The clock is frozen for the whole test, so NOTHING here can
    become "due" — the only thing that can paint the held text is the catch-up
    timer, and the assertion is that it does.

    The cross-section is taken while the reply is still OPEN (the completion
    frame is never sent — the LLM boundary below never returns), which is what
    makes it non-vacuous: the terminal completion writes the full text
    regardless and would make any post-completion assertion pass on the broken
    code too.

    Strip-falsify (PR body): removing the ``_schedule_streaming_catchup()`` call
    leaves the entry showing only the FIRST delta forever and this gate goes
    RED, while the coalescing gate above stays green.
    """
    clock = _Clock()
    released = asyncio.Event()

    async def _fake_llm(*_args, **kwargs):
        on_delta = kwargs["on_content_delta"]
        for _ in range(50):
            now = datetime.now().astimezone()
            on_delta(_CHUNK, raw_chunk_count=1, first_arrival=now, last_arrival=now)
        await released.wait()  # the reply never completes during the assertion
        return LLMToolCallResult(
            content=_CHUNK * 50, tool_calls=[], finish_reason="stop", usage=_USAGE
        )

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _fake_llm)
    registry, transport, app = await _build(tmp_path, clock=clock)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.submit_user_text("stream please")
            await _until(lambda: _streamed_entry(app) is not None)
            entry = _streamed_entry(app)

            # Everything after the first delta was deferred by the (frozen)
            # budget. The catch-up timer's own real interval is the actual
            # bound (dedicated-tested by the interval constant itself); this
            # waits on the real event it produces, unbounded (#3748 owner
            # policy) rather than re-implementing a second timeout around it.
            await _until(lambda: str(entry.item.text).count(_CHUNK) > 1)
        released.set()
    finally:
        released.set()
        await registry.shutdown()
