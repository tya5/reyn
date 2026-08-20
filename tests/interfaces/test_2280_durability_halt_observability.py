"""Tier 1/2: #2280 — durability-halt observability (idle-operator surface).

Triage found ``Session.halted_reason`` (runtime/session.py) had zero consumers:
the accept-edge (``Session._put_inbox``) already fail-stops synchronously
(raises ``DurabilityHaltError`` — the pre-existing SAFETY mechanism, untouched
here), but an operator who is IDLE (not currently submitting anything) had no
way to learn the session halted until their next interaction. Issue #2280 asks
for a proactive surface — this module gates that a `session_halted` audit-event
now fires the moment the fail-stop latches (on EITHER edge, guarded to fire
once), and that it reaches every operator-facing surface:

  - Gate 1 (producer): the process-edge halt (``Session.run_one_iteration`` —
    the exact "idle operator" path, no exception raised to anyone) emits the
    event, and a second check does not re-emit.
  - Gate 2 (TUI status line): ``interfaces.repl.status._snapshot`` +
    ``interfaces.inline.textual_chat.chrome.status_line_text`` surface the
    reason; a healthy session's status line carries no such text (negative
    control).
  - Gate 3 (plain --cui): ``bottom_toolbar`` on both renderers surfaces the
    reason once ``on_audit_event`` sees the event; a fresh renderer (no event
    yet) shows nothing (negative control). ``ConsoleChatRenderer`` is the LIVE
    ``--cui`` renderer (and, since #3292, also the LIVE renderer for
    ``chat.render_mode: plain`` on a TTY without ``--cui`` — that config now
    selects ``ConsoleChatRenderer`` too, not ``InlineChatRenderer``, genuine
    ``--cui`` equivalence). ``InlineChatRenderer`` — the class
    ``make_inline_renderer()`` constructs for the default interactive-TTY
    path, NOT the unwired legacy ``RichChatRenderer`` sibling — is covered
    here as a direct unit-level pin on its own ``bottom_toolbar`` contract,
    not a claim that this exact fallback is live-reachable post-#3292.
  - Gate 4 (reachable-for-purpose, TUI app level): a real ``TextualChatApp``
    driven by a real halted registry/session + a transport that forwards the
    `session_halted` event proactively (no DISPLAY frame involved at all,
    modelling a fully idle operator) ends up with "HALTED" in the ALWAYS-
    VISIBLE ``StatusLine`` widget's rendered text; the SAME setup with no halt
    shows no such text (negative control).

All gates use real instances (a real ``Session``/``StateLog``/``DurabilityWorker``
injected with a genuine persistent write failure — never a hand-set
``session._halted_reason`` — a real ``AgentRegistry``, real renderers, a real
``TextualChatApp`` + pilot) per the testing policy: no mocks, no private-state
assertions.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.core.events.durability_worker import DurabilityWorker
from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.renderer import ConsoleChatRenderer, InlineChatRenderer
from reyn.interfaces.repl.status import _snapshot
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame, Frame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DurabilityHaltError, Session
from reyn.schemas.models import Event
from tests._support.agent_session import make_session
from tests._support.events import settle


async def _inject_persistent_durability_failure(log: StateLog) -> None:
    """Genuinely trigger the §4-exhausted fire-and-forget durable-write
    failure that latches ``StateLog.durability_failed`` — the same real
    trigger ``tests/runtime/test_2259_pr3_recovery_semantics_falsify.py`` uses, never
    a private-attribute poke."""
    async def _boom() -> None:
        raise OSError("simulated disk death")

    log.submit_durable_nowait(_boom)
    await log.flush()
    assert log.durability_failed, "setup: the injected failure must latch durability_failed"


# ── Gate 1: the process-edge (idle-operator) halt emits `session_halted` ──────


@pytest.mark.asyncio
async def test_process_edge_halt_emits_session_halted_once(tmp_path) -> None:
    """Tier 2: the PROCESS-edge fail-stop (``run_one_iteration``, the exact
    path an IDLE operator's halt is discovered on — no exception raised to
    any caller) emits a ``session_halted`` audit-event carrying ``reason`` the
    moment it latches. A second process-edge check does not re-emit (guarded
    on ``halted_reason is None``). RED if the emit is absent: an idle
    operator would have no proactive signal at all, only ever the accept-edge
    raise on their own next submit."""
    worker = DurabilityWorker(max_write_attempts=1)  # fail-fast, no slow backoff
    log = StateLog(tmp_path / "wal.jsonl", worker=worker)
    session = make_session(agent_name="alpha", state_log=log)
    events: list[Event] = []
    await settle(session._audit_events)
    session.subscribe_audit_events(events.append)
    try:
        assert session.halted_reason is None
        await _inject_persistent_durability_failure(log)

        # Negative control: nothing halted-related emitted before the process
        # edge actually observes the failed health-signal.
        await settle(session._audit_events)
        assert not any(e.type == "session_halted" for e in events)

        cont = await session.run_one_iteration()
        assert cont is False, "the run loop must halt (process-edge)"
        assert session.halted_reason == "durability_failure"

        # Unpacking a 1-tuple is itself the "exactly one" behavioral assertion
        # (raises ValueError on zero or more than one) — never a len(...) pin.
        await settle(session._audit_events)
        (halted_event,) = [e for e in events if e.type == "session_halted"]
        assert halted_event.data.get("reason") == "durability_failure"

        # A second process-edge check (durability still dead) must NOT re-emit:
        # the SAME event object, not a fresh one appended.
        cont2 = await session.run_one_iteration()
        assert cont2 is False
        await settle(session._audit_events)
        (still_only_event,) = [e for e in events if e.type == "session_halted"]
        assert still_only_event is halted_event, "re-check re-emitted session_halted"
    finally:
        await log.aclose()


@pytest.mark.asyncio
async def test_accept_edge_halt_also_emits_session_halted_once(tmp_path) -> None:
    """Tier 2: the ACCEPT-edge (``_put_inbox`` — the pre-existing synchronous
    safety raise) latches the SAME emit, for a remote/other-window client
    watching passively while THIS operator is the one who triggered the
    accept-edge raise. The guard's OWN docstring names its target case as
    "keeps rejecting further ops ... every subsequent submit" — i.e. an
    operator (or a retrying client) who calls ``_put_inbox`` REPEATEDLY
    while still durability-dead, so the witness must call it twice (not
    once) and confirm the SECOND call does not re-emit (object identity,
    not a ``len(...) == N`` pin) — the single-call form used previously
    never exercised the guard at all (co-vet finding on #3336: it could not
    tell a guarded emit from an unguarded one)."""
    worker = DurabilityWorker(max_write_attempts=1)
    log = StateLog(tmp_path / "wal.jsonl", worker=worker)
    session = make_session(agent_name="alpha", state_log=log)
    events: list[Event] = []
    await settle(session._audit_events)
    session.subscribe_audit_events(events.append)
    try:
        await _inject_persistent_durability_failure(log)

        with pytest.raises(DurabilityHaltError):
            await session._put_inbox("user", {"text": "after disk death"})
        await settle(session._audit_events)
        (halted_event,) = [e for e in events if e.type == "session_halted"]
        assert halted_event.data.get("reason") == "durability_failure"

        # A second accept-edge submit (durability still dead, the operator's
        # retry) must NOT re-emit: the SAME event object, not a fresh one.
        with pytest.raises(DurabilityHaltError):
            await session._put_inbox("user", {"text": "still after disk death"})
        await settle(session._audit_events)
        (still_only_event,) = [e for e in events if e.type == "session_halted"]
        assert still_only_event is halted_event, "second submit re-emitted session_halted"
    finally:
        await log.aclose()


# ── Gate 2: the TUI status-line surface (status._snapshot -> chrome.status_line_text) ──


def _halted_registry(tmp_path):
    """A real AgentRegistry whose sole session is genuinely halted (process
    edge) — returns (registry, state_log) so the caller can ``aclose`` it."""
    holder: dict[str, StateLog] = {}

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        worker = DurabilityWorker(max_write_attempts=1)
        log = StateLog(agent_dir / "state" / "wal.jsonl", worker=worker)
        holder["log"] = log
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            state_log=log,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("solo")
    return reg, holder


@pytest.mark.asyncio
async def test_status_line_text_surfaces_halted_reason(tmp_path) -> None:
    """Tier 1: a halted session's status snapshot carries ``halted_reason``
    (``interfaces.repl.status._snapshot``) and the chrome formatter
    (``status_line_text``) prepends a HALTED banner ahead of the usual
    values — the ONE always-visible chrome region. RED if either the
    snapshot key or the chrome-side rendering is stripped."""
    from reyn.interfaces.inline.textual_chat.chrome import status_line_text

    reg, holder = _halted_registry(tmp_path)
    try:
        session = await reg.attach("solo")
        await _inject_persistent_durability_failure(holder["log"])
        cont = await session.run_one_iteration()
        assert cont is False
        assert session.halted_reason == "durability_failure"

        snap = _snapshot(reg)
        assert snap is not None
        assert snap["halted_reason"] == "durability_failure"

        text = status_line_text(snap, "solo")
        assert "HALTED" in text
        assert "durability_failure" in text
    finally:
        await reg.shutdown()


def test_status_line_text_healthy_session_shows_no_banner() -> None:
    """Tier 1: the negative control — a healthy (never-halted) snapshot's
    status line carries no HALTED banner. Without this, a banner that is
    simply always rendered would pass Gate 2 above trivially."""
    from reyn.interfaces.inline.textual_chat.chrome import status_line_text

    healthy_snap = {
        "model": "sonnet",
        "attached_name": "solo",
        "cost_agent": 0.0,
        "ctx_used": 0,
        "ctx_window": 100,
        "halted_reason": None,
    }
    text = status_line_text(healthy_snap, "solo")
    assert "HALTED" not in text
    # Also the "no snapshot at all" pre-session fallback.
    assert "HALTED" not in status_line_text(None, "solo")


# ── Gate 3: the plain --cui renderers' bottom_toolbar ──────────────────────────


def test_console_renderer_bottom_toolbar_surfaces_halt() -> None:
    """Tier 1: ``ConsoleChatRenderer`` (the ``--cui`` renderer) shows the halt
    reason in its persistent ``bottom_toolbar`` slot once ``on_audit_event``
    observes ``session_halted`` — the plain-path equivalent of the TUI status
    line, live even while the operator sits idle at the prompt."""
    renderer = ConsoleChatRenderer()
    assert renderer.bottom_toolbar() is None, "negative control: healthy renderer shows nothing"
    renderer.on_audit_event(Event(type="session_halted", data={"reason": "durability_failure"}))
    toolbar = renderer.bottom_toolbar()
    assert toolbar is not None
    assert "durability_failure" in toolbar
    assert "HALTED" in toolbar


def test_inline_renderer_bottom_toolbar_surfaces_halt() -> None:
    """Tier 1: ``InlineChatRenderer`` — the class ``make_inline_renderer()``
    actually constructs for the default interactive-TTY path (the legacy
    ``RichChatRenderer`` sibling class is never constructed by any production
    call site) — shows the same halt banner. Pre-#3292, this class was also
    what ran the plain PromptSession loop when ``chat.render_mode: plain`` was
    configured on a TTY without ``--cui``; #3292 made that config select
    ``ConsoleChatRenderer`` instead (genuine ``--cui`` equivalence), so this
    assertion is now a direct unit-level pin on ``bottom_toolbar``'s own
    contract, not a claim about that specific fallback's live reachability."""
    renderer = InlineChatRenderer()
    assert renderer.bottom_toolbar() is None, "negative control: healthy renderer shows nothing"
    renderer.on_audit_event(Event(type="session_halted", data={"reason": "durability_failure"}))
    toolbar = renderer.bottom_toolbar()
    assert toolbar is not None
    markup = toolbar.value
    assert "durability_failure" in markup
    assert "HALTED" in markup


# ── Gate 4: reachable-for-purpose — a real TextualChatApp's StatusLine ────────


class _QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` backed by an ``asyncio.Queue``
    so a test can push a frame WHILE the app's frame-pump worker is already
    running (mid-session), rather than only pre-seeding frames before mount —
    the shape needed to prove a mid-session proactive update, not merely that
    an already-halted session's FIRST snapshot happens to already show it."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[Frame]" = asyncio.Queue()

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def push(self, frame: "Frame") -> None:
        self._queue.put_nowait(frame)

    async def frames(self) -> "AsyncIterator[Frame]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:  # pragma: no cover - unused
        return ""

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover - unused
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.mark.asyncio
async def test_idle_operator_sees_halted_status_line_mid_session(tmp_path) -> None:
    """Tier 2: the end-to-end "idle operator" gate. The app mounts against a
    HEALTHY session first (proving the initial state carries no banner, not
    just that an already-halted session's first snapshot happens to show
    one) — THEN the session is genuinely halted (process edge) and its
    ``session_halted`` event is the ONLY frame pushed afterward (no DISPLAY
    frame, no user action — modelling an operator who submitted nothing and
    is not mid-turn). The ALWAYS-VISIBLE :class:`StatusLine` widget must
    proactively pick up the halt. RED if the app-side ``session_halted``
    handler is stripped: the status line would stay stale forever (nothing
    else ever triggers a refresh while idle)."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp
    from reyn.interfaces.repl.read_model import RegistryReadModel

    reg, holder = _halted_registry(tmp_path)
    try:
        session = await reg.attach("solo")
        transport = _QueueTransport()
        read_model = RegistryReadModel(reg)
        app = TextualChatApp(transport=transport, read_model=read_model, agent_name="solo")
        async with app.run_test() as pilot:
            await pilot.pause()
            text_before = str(app.query_one(StatusLine).render())
            assert "HALTED" not in text_before, f"must start healthy: {text_before}"

            await _inject_persistent_durability_failure(holder["log"])
            cont = await session.run_one_iteration()
            assert cont is False
            assert session.halted_reason == "durability_failure"

            transport.push(
                EventFrame(Event(type="session_halted", data={"reason": session.halted_reason}))
            )
            await pilot.pause()
            await pilot.pause()
            text_after = str(app.query_one(StatusLine).render())
            assert "HALTED" in text_after
            assert "durability_failure" in text_after
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_idle_healthy_session_status_line_shows_no_banner(tmp_path) -> None:
    """Tier 2: the negative control for Gate 4 — a healthy (never-halted)
    session with an idle transport (no frames at all) shows a normal status
    line with no HALTED text throughout. Without this, an always-on banner
    (a bug that would still pass the positive gate above) would go
    undetected."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp
    from reyn.interfaces.repl.read_model import RegistryReadModel

    reg, _holder = _halted_registry(tmp_path)
    try:
        await reg.attach("solo")
        transport = _QueueTransport()  # never pushed to — stays idle/healthy
        read_model = RegistryReadModel(reg)
        app = TextualChatApp(transport=transport, read_model=read_model, agent_name="solo")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            text = str(app.query_one(StatusLine).render())
            assert "HALTED" not in text
    finally:
        await reg.shutdown()
