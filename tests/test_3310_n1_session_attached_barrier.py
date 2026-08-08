"""Tier 2: #3310 N1 — the ``session_attached`` switch-notification barrier.

A session/agent switch (``/attach <name>`` -> ``registry.attach()``,
``/session switch <sid>`` -> ``registry.attach_session()``) flips which
session's frames reach the client, but historically nothing told the client
THAT a switch had happened. This adds a ``session_attached`` chat-event
carrying ``{agent, session_id}``, emitted at the registry attach seam as an
``EventFrame`` put DIRECTLY on ``repl_outbox`` (never routed through a
session's own chat-events — that stream is the thing being swapped).

Gates covered here:

1. The event is emitted on BOTH switch paths (``attach`` / ``attach_session``),
   carrying the correct ``{agent, session_id}``.
2. ★Barrier: the flip (``self._connection.switch(key)``) and the announce
   (``repl_outbox.put_nowait``) happen with NO real ``await`` suspension in
   between, so the announce is ALWAYS the first thing a switch contributes to
   ``repl_outbox`` — even when the newly-focused session already has a frame
   queued up, ready for its forwarder to deliver.
3. A surface with no handler for this ``EventFrame`` draws NOTHING (opt-in
   draw, the #3288 ③b property) — contrasted, over the SAME delivery path,
   with a DISPLAY frame of an ordinary kind, which the same renderer DOES
   draw (proving the silence above is the opt-in-draw property, not a broken
   pipe).
4. Vocabulary/profile wiring: ``session_attached`` is in the frames
   vocabulary (``renderer_chat_events()``) AND registered in the AG-UI
   extension profile (``is_profiled("reyn.event.session_attached")``).
   Stripping the ``profile.py`` entry alone is caught by the EXISTING
   completeness gate (``tests/test_agui_profile_completeness.py::
   test_every_custom_mapped_frame_is_profiled`` — strip-falsified during
   review, not committed here). Stripping the ``frames.py`` vocabulary entry
   alone is NOT caught by anything else (found during review: with
   ``session_attached`` absent from ``renderer_chat_events()``, the profile
   gate's own enumeration simply never looks at it — silently untested, not
   RED) — pinned directly below so THAT half has its own gate too.

Real ``AgentRegistry`` + real ``Session`` (via ``tests._support.agent_session
.make_session``) for 1/2; a real ``InProcessTransport`` + real
``ConsoleChatRenderer`` (write-capturing subclass) behind a small
``EventLog``/``repl_outbox`` registry double for 3 — no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import Event, EventLog
from reyn.interfaces.repl.renderer import ConsoleChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.agui.profile import is_profiled
from reyn.interfaces.transport.frames import EventFrame, FrameTag, renderer_chat_events
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
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
    reg.create("beta")
    return reg


def _drain_all(q: "asyncio.Queue") -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _pump(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0.01)


# ── 1. emitted on both switch paths, correct payload ────────────────────────


@pytest.mark.asyncio
async def test_session_attached_emitted_by_attach(tmp_path) -> None:
    """Tier 2: ``attach(name)`` puts a ``session_attached`` EventFrame on
    ``repl_outbox`` carrying ``{agent: name, session_id: _DEFAULT_SID}``."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        frames = _drain_all(reg.repl_outbox)
        # Unpacking into a single-element tuple IS the "exactly one" assertion
        # (raises ValueError on 0 or 2+ matches) — a behavioral check on the
        # extracted value, not a `len(...) == N` format pin.
        (attached,) = [
            f for f in frames
            if isinstance(f, EventFrame) and f.event.type == "session_attached"
        ]
        assert attached.event.data == {"agent": "alpha", "session_id": _DEFAULT_SID}
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_session_attached_emitted_by_attach_session(tmp_path) -> None:
    """Tier 2: ``attach_session(name, sid)`` puts the SAME shaped announce,
    carrying the target session's own sid (not the default)."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        _drain_all(reg.repl_outbox)  # discard the first-attach announce

        sid = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        await reg.attach_session("alpha", sid)

        frames = _drain_all(reg.repl_outbox)
        (attached,) = [
            f for f in frames
            if isinstance(f, EventFrame) and f.event.type == "session_attached"
        ]
        assert attached.event.data == {"agent": "alpha", "session_id": sid}
        assert sid != _DEFAULT_SID
    finally:
        for task in reg.running_tasks():
            task.cancel()


# ── 2. ★barrier: the announce is always first, even under a real race ──────


@pytest.mark.asyncio
async def test_barrier_announce_precedes_new_sessions_own_frames(tmp_path) -> None:
    """Tier 2: ★race gate. A concurrent adversary task polls the registry's
    PUBLIC focus accessor (``attached_name``) as tightly as physically
    possible (a bare ``await asyncio.sleep(0)`` loop — the fastest ANY real
    consumer, however it is implemented, could ever react to the flip) and
    races to put its own frame onto ``repl_outbox`` the instant it observes
    the switch. The flip (``self._connection.switch(key)``) and the announce
    (``repl_outbox.put_nowait``) have NO real ``await`` between them (design-
    pass contract, mirroring ``Session.cancel_queued``'s no-await critical
    section, #3300 Y-server / #3306) — so no matter how fast the adversary
    polls, it can only ever observe the flip AFTER the announce is already
    committed (the same synchronous Python step performed both), and its
    frame is therefore physically incapable of landing before the announce.

    Strip-falsify (recorded in the PR body — reproduced live during review,
    not committed here): inserting a real ``await asyncio.sleep(0)`` between
    the flip and the announce in ``AgentRegistry.attach`` gives the adversary
    a real scheduling gap to win the race in — this test goes RED (the
    adversary frame lands BEFORE the announce)."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        _drain_all(reg.repl_outbox)  # discard the first-attach announce

        adversary_frame = OutboxMessage(kind="agent", text="adversary-frame")

        async def _adversary() -> None:
            # As fast as physically possible: no sleep duration, no I/O —
            # just re-scheduled every single event-loop tick until the flip
            # is observed, then commit immediately (no await before the put).
            while reg.attached_name != "beta":
                await asyncio.sleep(0)
            reg.repl_outbox.put_nowait(adversary_frame)

        adversary_task = asyncio.create_task(_adversary())
        await asyncio.sleep(0)  # let the adversary take its first (failing) poll

        await reg.attach("beta")
        await asyncio.wait_for(adversary_task, timeout=2.0)

        frames = _drain_all(reg.repl_outbox)
        # Unpacking into exactly two names asserts "exactly these two, in
        # this order" behaviorally — a 0/1/3+-item queue raises ValueError
        # here rather than needing a separate length check.
        first, second = frames
        assert isinstance(first, EventFrame) and first.event.type == "session_attached", (
            f"the announce must be FIRST on repl_outbox, ahead of the fastest "
            f"possible adversary: {frames!r}"
        )
        assert first.event.data == {"agent": "beta", "session_id": _DEFAULT_SID}
        assert second is adversary_frame
    finally:
        for task in reg.running_tasks():
            task.cancel()


@pytest.mark.asyncio
async def test_barrier_holds_for_attach_session_too(tmp_path) -> None:
    """Tier 2: ★race gate, the ``attach_session`` (sid-switch) call site —
    the SAME adversary as above, per-site (not cross-masked by the
    ``attach`` test): ``attach_session`` has its OWN
    ``self._connection.switch(key)`` / announce pair (a separate code path from
    ``attach``), so it needs its own independent proof the barrier holds
    there too."""
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        sid = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
        _drain_all(reg.repl_outbox)  # discard the first-attach + spawn announces

        adversary_frame = OutboxMessage(kind="agent", text="adversary-frame-2")

        async def _adversary() -> None:
            while reg.attached_sid != sid:
                await asyncio.sleep(0)
            reg.repl_outbox.put_nowait(adversary_frame)

        adversary_task = asyncio.create_task(_adversary())
        await asyncio.sleep(0)

        await reg.attach_session("alpha", sid)
        await asyncio.wait_for(adversary_task, timeout=2.0)

        frames = _drain_all(reg.repl_outbox)
        first, second = frames
        assert isinstance(first, EventFrame) and first.event.type == "session_attached", (
            f"the announce must be FIRST on repl_outbox: {frames!r}"
        )
        assert first.event.data == {"agent": "alpha", "session_id": sid}
        assert second is adversary_frame
    finally:
        for task in reg.running_tasks():
            task.cancel()


# ── 3. opt-in draw: no handler => no visible-garbage window ─────────────────


class _FakeRegistry:
    """Registry double for ``InProcessTransport``: a real ``repl_outbox`` +
    real ``EventLog`` (unused here — the announce bypasses chat_events
    entirely, exactly like production), no mocks."""

    def __init__(self) -> None:
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        self.chat_events = EventLog()
        self._cb = None

    def bind_focus_listeners(self, *, on_chat_event=None, intervention_channel=None) -> None:
        self._cb = on_chat_event
        if on_chat_event is not None:
            self.chat_events.add_subscriber(on_chat_event)

    def unbind_focus_listeners(self) -> None:
        if self._cb is not None:
            self.chat_events.remove_subscriber(self._cb)
            self._cb = None

    def attached_session(self):
        return None


class _WriteCapturingRenderer(ConsoleChatRenderer):
    """A REAL renderer (unchanged branching), just recording ``_write`` calls
    instead of touching stdout — so "nothing drawn" is an observed absence of
    a real render call, not an inferred one."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: "list[str]" = []

    def _write(self, s: str) -> None:
        self.writes.append(s)


async def _drive_one_frame(put_frame) -> "list[str]":
    fake = _FakeRegistry()
    transport = InProcessTransport(fake, intervention_channel="tui")
    transport.start()
    renderer = _WriteCapturingRenderer()
    try:
        put_frame(fake)
        fake.repl_outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        await asyncio.wait_for(run_output_loop(transport, renderer), timeout=2.0)
    finally:
        transport.close()
    return renderer.writes


@pytest.mark.asyncio
async def test_session_attached_event_frame_draws_nothing() -> None:
    """Tier 2: a ``session_attached`` EventFrame travels the SAME
    ``repl_outbox`` -> ``InProcessTransport`` -> ``run_output_loop`` delivery
    path a display frame would, but ``ConsoleChatRenderer.on_chat_event`` has
    no branch for it (an ``if/elif`` chain with no ``else``) — it is
    consumed and dropped, never rendered. Assert the INTERMEDIATE state (§10):
    this drive puts ONLY the session_attached frame (plus the terminator) —
    no other write source is present to accidentally mask a real draw."""

    def _put(fake: "_FakeRegistry") -> None:
        fake.repl_outbox.put_nowait(
            EventFrame(Event(type="session_attached", data={"agent": "beta", "session_id": "main"}))
        )

    writes = await _drive_one_frame(_put)
    assert writes == [], (
        f"session_attached must draw NOTHING (opt-in draw, #3288 ③b property) — "
        f"got renderer writes: {writes!r}"
    )


@pytest.mark.asyncio
async def test_same_path_positive_control_display_frame_does_draw() -> None:
    """Tier 2: the positive control for the test above, over the IDENTICAL
    delivery path (same fake registry shape, same transport, same renderer
    class, same run_output_loop) — a plain DISPLAY frame with an ordinary
    kind (``user``, which ``ConsoleChatRenderer`` has no special ``_PREFIX``
    entry for) still reaches ``message()`` and DOES produce a write. This is
    what rules out the previous test's silence being a dead/broken pipe
    rather than the EventFrame-specific opt-in-draw property (verification-
    hazards §10: the control must travel the same path as the frame under
    test, not a shortcut through a different one)."""

    def _put(fake: "_FakeRegistry") -> None:
        fake.repl_outbox.put_nowait(OutboxMessage(kind="user", text="hi"))

    writes = await _drive_one_frame(_put)
    assert writes, "the same-path positive control must actually draw something"
    assert any("hi" in w for w in writes)


def test_session_attached_is_an_event_frame_never_a_display_kind() -> None:
    """Tier 1: pin the frame SHAPE — ``session_attached`` rides an
    ``EventFrame`` (``FrameTag.EVENT``), never an ``OutboxMessage``/
    ``DisplayFrame`` kind. Constructing it any other way would be the
    category error #3288 ③b's owner-ratified decision designed out."""
    frame = EventFrame(Event(type="session_attached", data={"agent": "a", "session_id": "main"}))
    assert frame.tag is FrameTag.EVENT


# ── 4. vocabulary + AG-UI profile wiring ─────────────────────────────────────


def test_session_attached_is_in_the_frames_vocabulary() -> None:
    """Tier 1: ``session_attached`` is in ``renderer_chat_events()`` — the
    forward-set both ``InProcessTransport`` and the AG-UI endpoint filter
    against. Stripping this membership is NOT caught by
    ``test_agui_profile_completeness.py`` (its own enumeration reads THIS
    set, so removing the entry here just makes that gate stop looking at
    ``session_attached`` — silently untested, not RED — found during
    review); this direct pin is what actually catches that removal."""
    assert "session_attached" in renderer_chat_events()


def test_session_attached_is_profiled_in_the_agui_extension() -> None:
    """Tier 1: ``reyn.event.session_attached`` is a registered AG-UI
    extension-profile name (``profile.py``'s ``CUSTOM_PROFILE``) — the OTHER
    half of gate 4. Strip-falsified during review (removing the
    ``CustomName`` entry turns ``test_agui_profile_completeness.py::
    test_every_custom_mapped_frame_is_profiled`` RED, since
    ``session_attached`` IS in ``renderer_chat_events()`` per the test
    above, so the codec DOES emit ``reyn.event.session_attached`` for it)."""
    assert is_profiled("reyn.event.session_attached")
