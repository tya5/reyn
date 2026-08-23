"""Tier 2: #3310 N3 — remote (AG-UI) parity for session switch.

N1 (#3321) gave the LOCAL registry seam a ``session_attached`` barrier
audit-event on ``repl_outbox``. A REMOTE AG-UI client never sees it: the
emitter's per-connection ``_SessionFrameSource`` reads a session's own
``outbox_hub``/``audit_events`` directly (never ``registry.repl_outbox``), and
is bound to ONE session object for the SSE connection's lifetime — so a
switch left it stranded on the OLD session, and even if it followed, the
emitter's ``MESSAGES_SNAPSHOT`` backlog is fixed at connect time (#3288/#3300
era design). Two premises, both re-verified here structurally by construction
(not merely asserted): a remote client that switches sessions had NO way to
obtain the new session's scrollback at all.

The fix (N3, ported off the sentinel #4534 PR-2b): ``_SessionFrameSource``
subscribes to ``registry.add_attach_listener`` — the registry fires it,
synchronously, from the SAME no-await critical section
``_announce_session_attached`` uses, whenever ``attach``/``attach_session``
switches focus for the watched agent name. The listener hands the target sid
to a per-connection ``asyncio.Queue`` (``_switch_signal``) that
``_drain_one_session``'s drain loop dual-waits alongside its outbox
subscription (``asyncio.wait(..., return_when=FIRST_COMPLETED)``) — the
second wait source an in-flight blocked ``await sub.get()`` needs to be
interrupted by an out-of-band signal (the switch no longer arrives in-band on
that same subscription, unlike the retired sentinel). On seeing it, the
source re-points itself at the target session and synthesizes a
``session_attached`` EventFrame onto its OWN per-connection queue.
``AgUiEmitter``, on observing that EventFrame, treats the switch as a
*logical reconnect*: it re-fires the SAME ``MESSAGES_SNAPSHOT``/
``STATE_SNAPSHOT`` protocol it uses at connect
(:meth:`AgUiEmitter._reconnect_snapshot_chunks`), sourcing the new backlog from
a caller-supplied ``backlog_provider`` — here, ``session_backlog_frames``,
which projects a session's in-memory ``ChatMessage`` history through the SAME
``project_restored_frames`` SSoT local restore-on-restart uses (#3273 P5).

These tests drive the switch via ``registry.attach_session`` directly — the
real op both the retired local-REPL sentinel path and the current
``ClientTransport.request_session_switch`` seam call into; they exercise
``_SessionFrameSource``'s own reconnect/backlog/ordering mechanics, not the
transport-to-registry wiring. The full stack, through a real ASGI POST on an
ALREADY-OPEN SSE stream, is
``tests/interfaces/test_4534_pr2b_switch_follow_e2e.py``'s own claim.

Gates:

1. ★Remote parity (+staleness): A -> B -> A over the real server pipeline
   (``_SessionFrameSource`` + ``AgUiEmitter`` + real SSE text + real
   ``AgUiTransport`` decode) reconstructs the SAME view a local hydrate
   (``project_restored_frames`` read straight off ``session.history``) would
   — including content that entered B's history while the connection was
   elsewhere (no caching: the backlog is read fresh at switch time). Strip
   the re-fire (``backlog_provider=None``) -> the remote view is missing the
   scrollback after a switch -> RED (asserted directly below).
2. Re-fire ordering: the switch's ``session_attached`` SSE event precedes its
   ``MESSAGES_SNAPSHOT``/``STATE_SNAPSHOT`` re-fire, never the reverse.
3. Connect path unchanged: an ordinary single-session connection (no switch
   ever occurs) emits byte-identical SSE text whether or not
   ``backlog_provider`` is wired — the re-fire is dormant unless a
   ``session_attached`` event actually flows through.
4. No per-client "which frames have I already seen" bookkeeping: the fix is
   re-subscription (WHICH session a connection currently reads from) plus a
   fresh read of that session's live history at switch time — never a set of
   previously-delivered frame/message ids consulted before forwarding.

★co-vet follow-up on this PR (#3322):

(a) The announce is enqueued onto this connection's queue BEFORE
``_bind(target)`` makes the new session's audit-event subscriber live (moved,
2-line reorder) — an audit-event the new session emits synchronously cannot
reach the queue before its subscriber exists, so "barrier precedes any of the
new session's own frames" holds BY CONSTRUCTION, not merely because there is
currently no ``await`` between the two steps. Witnessed by an adversary that
floods the target session's OWN audit-event stream starting the instant the
switch is triggered (``test_switch_announce_precedes_any_new_session_audit_event``).

(b) The strip-falsify RED for gate 1 is bounded and assertion-based, not a
120s-timeout hang: collection helpers below (``_collect_sse_within`` /
``_collect_frames_within``) read for a fixed wall-clock window rather than
awaiting a stream that — when the fix is absent — never terminates (the
connection is permanently stranded on the old session), so a broken build
fails FAST with an assertion naming what did/did not arrive.

Real ``AgentRegistry`` + real ``Session`` (``tests._support.agent_session
.make_session``), the real ``_SessionFrameSource`` / ``AgUiEmitter`` /
``AgUiTransport`` / SSE codec — no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    _SessionFrameSource,
    session_backlog_frames,
)
from reyn.interfaces.transport.agui.protocol import parse_sse_blocks
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame, EventFrame
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from tests._support.agent_session import make_session


def _registry(tmp_path):
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


async def _pump(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0.01)


async def _sse_lines(text):
    # #5139: an explicit yield per line — this test builds its WHOLE
    # ``sse`` text up-front (``_collect_all``, no real network between
    # events), unlike production, which decodes bytes as they actually
    # arrive with real gaps. A ``BacklogBatch`` (#5139's own item, now
    # queued through the SAME ``out``/display-queue every live frame
    # flows through — no side channel) gets its own
    # :func:`~reyn.interfaces.transport.drain.suspend_between_frames`
    # checkpoint like any other item, so this is stronger than strictly
    # required today — kept anyway: it is what guarantees this test's
    # OWN two switch re-fires (A's connect-time backlog, then B's
    # switch-time re-fire) are never coalesced into one queue burst by an
    # SSE source with no gaps of its own.
    for line in text.split("\n"):
        yield line
        await asyncio.sleep(0)


def _apply_client_side(events: "list", *, on_display, on_reset) -> None:
    """The minimal client-side reaction any compliant consumer (N2's actual
    implementation, or this test's stand-in) applies: clear on the barrier,
    append display text otherwise. Proves the WIRE carries the right frames
    in the right order — not a re-implementation of N2's own reset logic."""
    for frame in events:
        if isinstance(frame, EventFrame) and getattr(frame.event, "type", "") == "session_attached":
            on_reset()
        elif isinstance(frame, DisplayFrame):
            on_display(frame.message)


def _texts(msgs: "list[OutboxMessage]") -> "list[str]":
    return [f"{m.kind}:{m.text}" for m in msgs]


async def _collect_all(agen) -> "list":
    """Collect every item from an async generator until it terminates.

    #4275 (co-vet #3322 (b) is superseded, not just amended — that review's
    "collecting unboundedly turns a structural break into an undiagnosable
    CI-timeout hang" is exactly the justification the #4145 owner ruling
    names and rejects: a local bounded window duplicates CI's own kill
    switch and adds an arbitrary failure constant). Every caller here
    deliberately pushes an ``__end__`` sentinel, which ``AgUiEmitter.stream()``
    returns on, so the stream is naturally finite. If a switch-follow
    mechanism regresses and the stream never terminates, this hangs,
    surfaced by CI's kill switch rather than a local window that would
    silently truncate the collected list instead of failing.
    """
    return [item async for item in agen]


async def _collect_until_barrier(agen) -> "list":
    """Collect items off an async generator until the ``session_attached``
    barrier frame is dequeued — the one deterministic thing this test needs.

    #4280 ②: ``source.frames()`` has no ``__end__``-style natural EOF (unlike
    ``AgUiEmitter.stream()``, covered by ``_collect_all`` above), so a
    wall-clock window (the prior form) was the only stand-in — the exact
    "bounded because the alternative is an undiagnosable hang" justification
    the #4145 ruling rejects. The real termination condition is available
    though: the barrier frame is guaranteed to arrive exactly once (real
    switch-follow production behaviour), and this test's whole point is
    checking what arrived BEFORE it — so once it is dequeued, everything
    relevant is already in ``out`` (the queue is drained strictly in order,
    and the adversary task has already fully run by the time collection
    starts, so no adversary frame can still be in flight)."""
    out: list = []
    it = agen.__aiter__()
    while True:
        item = await it.__anext__()
        out.append(item)
        if isinstance(item, EventFrame) and getattr(item.event, "type", "") == "session_attached":
            return out


async def _run_emitter_to_frames(emitter: AgUiEmitter) -> "list":
    """#5139 (architect FINAL ruling, issuecomment-5383272756): a
    ``MessagesSnapshot`` backlog now arrives as ONE ``BacklogBatch`` item
    IN ``AgUiTransport.frames()``'s own stream — no side channel, no pop.
    This helper's own CONTRACT (a flat, ordered "what a client would
    render" list) stays unchanged for every caller below — flattening
    each batch's ``.frames`` back into the SAME flat list here, mirroring
    ``TextualChatApp._pump_frames``'s own handling (apply the moment it
    is dequeued — wire order already IS apply order under this design,
    nothing to reorder) — rather than pushing that reconstruction onto
    each test. ``agent_name="alpha"`` matches every caller's own registry
    fixture (``_registry`` above always creates agent "alpha") so the
    FIRST connect's own batch — which has no preceding
    ``session_attached`` announce to read a destination off, see
    ``AgUiTransport.__init__``'s own comment — carries the wire's real
    agent name instead of the constructor default's empty string."""
    sse = "".join(await _collect_all(emitter.stream()))

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send, agent_name="alpha")
    out: "list" = []
    async for f in transport.frames():
        if isinstance(f, BacklogBatch):
            out.extend(f.frames)
        else:
            out.append(f)
    return out


@pytest.mark.asyncio
async def test_remote_switch_has_no_scrollback_without_the_refire(tmp_path) -> None:
    """Tier 2: premise check — with ``backlog_provider=None`` (the pre-N3
    shape), a switch's ``session_attached`` event reaches the wire (N3's
    re-subscription still works) but NO backlog follows it: the remote view
    is provably missing the scrollback, the exact gap this PR closes."""
    reg = _registry(tmp_path)
    try:
        session_a = await reg.attach("alpha")
        sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        session_b = reg.get_session("alpha", sid_b)
        session_b.history.append(ChatMessage(role="assistant", content="b's own reply"))

        source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
        source.start()
        emitter = AgUiEmitter(source.frames(), lambda: None)  # no backlog_provider

        async def _switch() -> None:
            await _pump(2)
            await reg.attach_session("alpha", sid_b)
            await _pump(5)
            await session_b._put_outbox(OutboxMessage(kind="__end__", text=""))

        switch_task = asyncio.create_task(_switch())
        try:
            frames = await _run_emitter_to_frames(emitter)
        finally:
            await switch_task
            source.close()

        # The barrier DID arrive (N3's re-subscription is independent of the
        # re-fire) ...
        barrier_frames = [
            f for f in frames
            if isinstance(f, EventFrame) and getattr(f.event, "type", "") == "session_attached"
        ]
        assert barrier_frames, (
            f"expected a session_attached EventFrame within the collection "
            f"window; none arrived. Collected {len(frames)} frame(s): {frames!r}"
        )
        # ... but no DisplayFrame carrying B's content is anywhere on the wire —
        # RED: a remote client here has no way to obtain B's scrollback at all.
        b_content_frames = [
            f for f in frames
            if isinstance(f, DisplayFrame) and "b's own reply" in f.message.text
        ]
        assert not b_content_frames, (
            f"expected NO display frame carrying B's content (pre-refire "
            f"wiring has nothing to send it); got {b_content_frames!r}"
        )
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_remote_switch_parity_a_b_a_with_staleness(tmp_path) -> None:
    """Tier 2: ★A -> B -> A over the real wire reconstructs the SAME view a
    local hydrate would, INCLUDING content B accrued while this connection was
    elsewhere (staleness gate — no cache, backlog read fresh at switch time)."""
    reg = _registry(tmp_path)
    try:
        session_a = await reg.attach("alpha")
        session_a.history.append(ChatMessage(role="user", content="hello A"))
        session_a.history.append(ChatMessage(role="assistant", content="hi, I'm A"))

        sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        session_b = reg.get_session("alpha", sid_b)
        # B's content exists BEFORE the connection ever switches to it — and
        # more arrives on ``session_b`` while the connection is still on A,
        # simulating "B had a turn while I was away" (no live subscriber
        # needed for this to reach the switch-time backlog fetch, since the
        # fetch reads ``session.history`` fresh, not a cached copy).
        session_b.history.append(ChatMessage(role="user", content="hello B"))
        session_b.history.append(ChatMessage(role="assistant", content="hi, I'm B"))

        source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
        source.start()

        async def _backlog_provider(name: str, sid: str):
            # #5139 C: AgUiEmitter's backlog_provider contract is now
            # async, returning (frames, has_more, next_cursor) — this
            # test's own oracle (session_backlog_frames, unbounded) has
            # no pagination of its own, so it always reports "no more"
            # (matches production's real ``session_backlog_page`` shape
            # when a page fits whole).
            return session_backlog_frames(reg, name, sid), False, None

        emitter = AgUiEmitter(source.frames(), lambda: None, backlog_provider=_backlog_provider)

        async def _drive() -> None:
            await _pump(2)
            await reg.attach_session("alpha", sid_b)
            await _pump(5)
            # A's history grows a SECOND turn while the connection is on B —
            # switching back to A must show it (fresh read, not stale cache).
            session_a.history.append(ChatMessage(role="user", content="are you there A"))
            await reg.attach_session("alpha", _DEFAULT_SID)
            await _pump(5)
            await session_a._put_outbox(OutboxMessage(kind="__end__", text=""))

        drive_task = asyncio.create_task(_drive())
        try:
            frames = await _run_emitter_to_frames(emitter)
        finally:
            await drive_task
            source.close()

        remote_view: "list[OutboxMessage]" = []
        _apply_client_side(
            frames, on_display=remote_view.append, on_reset=remote_view.clear
        )

        # The LOCAL oracle: a hydrate straight off session_a's CURRENT history
        # (what a local client's ``_hydrate_from_history``-style reset would
        # show after landing back on A) — same SSoT, direct read, no wire.
        local_view = session_backlog_frames(reg, "alpha", _DEFAULT_SID)
        local_texts = _texts([f.message for f in local_view])
        remote_texts = _texts(remote_view)

        assert remote_texts == local_texts, (
            f"remote reconstruction (A->B->A over the wire) diverged from "
            f"the local oracle (a direct hydrate off session_a.history):\n"
            f"remote={remote_texts!r}\nlocal={local_texts!r}"
        )
        # Sanity: the staleness content genuinely made it through, on BOTH sides.
        assert any("are you there A" in t for t in local_texts), (
            f"the local oracle itself is missing the staleness content — "
            f"local={local_texts!r}"
        )
        assert any("are you there A" in t for t in remote_texts), (
            f"the remote reconstruction is missing the staleness content "
            f"('are you there A', appended to session_a.history WHILE the "
            f"connection was on B) — remote={remote_texts!r}"
        )
        # And B's content is NOT mixed into the final (post switch-back) view.
        assert not any("hello B" in t for t in remote_texts), (
            f"B's content leaked into the post switch-back remote view — "
            f"remote={remote_texts!r}"
        )
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_rapid_back_to_back_switches_lose_no_backlog(tmp_path) -> None:
    """Tier 2: #5139 regression witness — the CONFIRMED production race
    (lead-coder, issuecomment-5383243289: "初回 connect の backlog が一度も
    pop されず、2 つ目の switch の backlog に上書きされて消えた" — a real
    uvicorn+httpx repro, since deleted as a permanent test because it
    exercised the now-REMOVED ``_pending_backlog`` overwrite slot
    directly). That slot is gone under the #5139 redesign: every decoded
    ``MessagesSnapshot`` is now its OWN discrete
    :class:`~reyn.interfaces.transport.frames.BacklogBatch` queue item,
    never a mutable single-slot side channel a second decode can
    overwrite before the first is drained — so there is no longer a
    SHARED slot for two switches to race on.

    A -> B -> A, back to back, with ZERO scheduling gap between the two
    ``attach_session`` calls (``_pump(0)``, unlike the sibling A->B->A
    test above, which pumps several times between steps) — the shape
    most likely to queue BOTH switches' ``MessagesSnapshot`` blocks
    before either is drained, the exact condition the original race
    needed. Strip-falsifier: making ``_consume_block``'s
    ``MessagesSnapshot`` branch a no-op (never emit a ``BacklogBatch`` at
    all — the observable end state a pre-#5139 overwrite-before-pop loss
    converges to: a batch nobody ever applies) turns this test red —
    verified locally (``remote=[]``, diverging from the local oracle),
    not merely reasoned about."""
    reg = _registry(tmp_path)
    try:
        session_a = await reg.attach("alpha")
        session_a.history.append(ChatMessage(role="user", content="hello A"))

        sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        session_b = reg.get_session("alpha", sid_b)
        session_b.history.append(ChatMessage(role="user", content="hello B"))

        source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
        source.start()

        async def _backlog_provider(name: str, sid: str):
            # #5139 C: AgUiEmitter's backlog_provider contract is now
            # async, returning (frames, has_more, next_cursor) — this
            # test's own oracle (session_backlog_frames, unbounded) has
            # no pagination of its own, so it always reports "no more"
            # (matches production's real ``session_backlog_page`` shape
            # when a page fits whole).
            return session_backlog_frames(reg, name, sid), False, None

        emitter = AgUiEmitter(source.frames(), lambda: None, backlog_provider=_backlog_provider)

        async def _drive() -> None:
            await _pump(1)
            await reg.attach_session("alpha", sid_b)
            await reg.attach_session("alpha", _DEFAULT_SID)  # no pump in between
            await _pump(5)
            await session_a._put_outbox(OutboxMessage(kind="__end__", text=""))

        drive_task = asyncio.create_task(_drive())
        try:
            frames = await _run_emitter_to_frames(emitter)
        finally:
            await drive_task
            source.close()

        remote_view: "list[OutboxMessage]" = []
        _apply_client_side(
            frames, on_display=remote_view.append, on_reset=remote_view.clear
        )

        local_view = session_backlog_frames(reg, "alpha", _DEFAULT_SID)
        local_texts = _texts([f.message for f in local_view])
        remote_texts = _texts(remote_view)

        assert remote_texts == local_texts, (
            f"rapid back-to-back A->B->A (zero scheduling gap) diverged "
            f"from the local oracle — a batch was lost or leaked:\n"
            f"remote={remote_texts!r}\nlocal={local_texts!r}"
        )
        assert any("hello A" in t for t in remote_texts), (
            f"A's own content is missing from the final view — "
            f"remote={remote_texts!r}"
        )
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_switch_announce_precedes_any_new_session_audit_event(tmp_path) -> None:
    """Tier 2: ★co-vet #3322 (a). The ``session_attached`` announce reaches
    this connection's own frame queue BEFORE the target session's audit-event
    subscriber goes live — never after.

    An adversary floods session B's OWN audit-event stream with real
    ``turn_started`` events (a type in the renderer-forwarded set) starting
    the INSTANT the switch request is queued. Such an event can only ever
    reach ``_q`` once B's subscriber is live (``add_subscriber`` does not
    replay past emits) — so if even ONE adversary event lands in ``_q``, its
    position relative to the barrier is a direct witness of whether the
    announce-before-subscribe ordering holds, independent of whether there
    happens to be a real scheduling gap between the two steps today."""
    reg = _registry(tmp_path)
    try:
        session_a = await reg.attach("alpha")
        sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        session_b = reg.get_session("alpha", sid_b)

        source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
        source.start()

        async def _adversary() -> None:
            for _ in range(200):
                session_b._audit_events.emit("turn_started")
                await asyncio.sleep(0)

        adversary_task = asyncio.create_task(_adversary())
        await reg.attach_session("alpha", sid_b)
        await adversary_task

        try:
            collected = await _collect_until_barrier(source.frames())
        finally:
            source.close()

        barrier_positions = [
            i for i, f in enumerate(collected)
            if isinstance(f, EventFrame) and getattr(f.event, "type", "") == "session_attached"
        ]
        adversary_positions = [
            i for i, f in enumerate(collected)
            if isinstance(f, EventFrame) and getattr(f.event, "type", "") == "turn_started"
        ]
        assert barrier_positions, (
            f"the session_attached barrier never reached this connection's "
            f"queue within the collection window; collected "
            f"{len(collected)} frame(s): {collected!r}"
        )
        (barrier_pos,) = barrier_positions
        if adversary_positions:
            assert barrier_pos < adversary_positions[0], (
                f"an adversary turn_started event from the NEW session "
                f"landed in the queue at position {adversary_positions[0]}, "
                f"BEFORE the session_attached barrier at position "
                f"{barrier_pos} — a client that resets its view on the "
                f"barrier would still miss this frame"
            )
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_barrier_precedes_refire_on_the_wire(tmp_path) -> None:
    """Tier 2: ★ordering gate. The switch's ``session_attached`` SSE event is
    emitted strictly BEFORE its MESSAGES_SNAPSHOT/STATE_SNAPSHOT re-fire —
    never after (a client that resets its view on the barrier must never see
    the reset race the very state the re-fire is about to deliver)."""
    reg = _registry(tmp_path)
    try:
        session_a = await reg.attach("alpha")
        sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        session_b = reg.get_session("alpha", sid_b)
        session_b.history.append(ChatMessage(role="assistant", content="b's reply"))

        source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
        source.start()

        async def _backlog_provider(name: str, sid: str):
            # #5139 C: AgUiEmitter's backlog_provider contract is now
            # async, returning (frames, has_more, next_cursor) — this
            # test's own oracle (session_backlog_frames, unbounded) has
            # no pagination of its own, so it always reports "no more"
            # (matches production's real ``session_backlog_page`` shape
            # when a page fits whole).
            return session_backlog_frames(reg, name, sid), False, None

        emitter = AgUiEmitter(source.frames(), lambda: None, backlog_provider=_backlog_provider)

        async def _switch() -> None:
            await _pump(2)
            await reg.attach_session("alpha", sid_b)
            await _pump(5)
            await session_b._put_outbox(OutboxMessage(kind="__end__", text=""))

        switch_task = asyncio.create_task(_switch())
        try:
            sse = "".join(await _collect_all(emitter.stream()))
        finally:
            await switch_task
            source.close()

        # Raw SSE event order (server-encoded, pre-client-decode) — the
        # sequence a strip (emitting the re-fire before the barrier) would
        # invert. CUSTOM-event ``name`` is the reliable signal for the
        # session_attached barrier (protocol.py's CUSTOM encoding shape:
        # ``{"name": f"reyn.event.{etype}", "value": edata}``).
        events = parse_sse_blocks(sse.split("\n"))
        barrier_positions = [
            i for i, ev in enumerate(events)
            if ev.type == "CUSTOM" and ev.data.get("name") == "reyn.event.session_attached"
        ]
        snapshot_positions = [
            i for i, ev in enumerate(events) if ev.type in ("MESSAGES_SNAPSHOT", "STATE_SNAPSHOT")
        ]
        # There are TWO of each: the initial connect (no barrier before it)
        # and the switch re-fire (barrier before it). The switch instance is
        # the SECOND occurrence of each on the wire. Unpacking into a
        # single-element tuple IS the "exactly one" assertion (raises on 0 or
        # 2+ matches) — a behavioral check on the extracted value, not a
        # ``len(...) == N`` format pin.
        assert barrier_positions, (
            f"expected exactly one session_attached CUSTOM event on the "
            f"wire; found none among {len(events)} event(s): {events!r}"
        )
        (barrier_pos,) = barrier_positions
        switch_messages_pos = snapshot_positions[len(snapshot_positions) // 2]
        assert barrier_pos < switch_messages_pos, (
            f"the switch re-fire (MESSAGES_SNAPSHOT/STATE_SNAPSHOT at "
            f"position {switch_messages_pos}) landed BEFORE the "
            f"session_attached barrier (position {barrier_pos}) — a client "
            f"resetting on the barrier would race the very state this "
            f"re-fire just delivered"
        )
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_connect_path_unaffected_by_backlog_provider_wiring(tmp_path) -> None:
    """Tier 2: connect path unchanged — an ordinary connection that NEVER
    switches sessions emits the SAME SSE event sequence whether or not
    ``backlog_provider`` is wired (the re-fire is dormant with no
    ``session_attached`` event ever flowing through it)."""

    async def _frames():
        yield DisplayFrame(OutboxMessage(kind="agent", text="hello"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    without = AgUiEmitter(_frames(), lambda: None)
    async def _empty_backlog_provider(a, s):
        return [], False, None

    with_provider = AgUiEmitter(_frames(), lambda: None, backlog_provider=_empty_backlog_provider)

    sse_without = "".join([c async for c in without.stream()])
    sse_with = "".join([c async for c in with_provider.stream()])

    # Compare structurally, ignoring the per-stream random ``messageId`` (a
    # UUID minted fresh by EACH ``AgUiEmitter`` instance — never a
    # ``backlog_provider`` effect, since neither stream here ever produces a
    # ``session_attached`` event to re-fire on): same event types, same
    # payload for every OTHER key.
    def _normalized(sse: str) -> "list[tuple[str, dict]]":
        out = []
        for ev in parse_sse_blocks(sse.split("\n")):
            data = {k: v for k, v in ev.data.items() if k != "messageId"}
            out.append((ev.type, data))
        return out

    assert _normalized(sse_without) == _normalized(sse_with)
