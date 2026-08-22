"""Tier 2: #5050 ③ — a remote client presents choices (and can answer
``always``) for an intervention it was never around to see the live
announce for.

Real chain (owner repro, `reyn chat --connect`): #5053 landed ①② —
``STATE_SNAPSHOT``/``STATE_DELTA`` carry ``pending_intervention_head``,
and ``RemoteReadModel.intervention_head()`` returns it instead of an
unconditional ``None``. Nothing consumed that method before this fix
(measured by grep, #5050's own issue thread) — ``app.py``'s
``_present_hydrated_intervention_head_if_pending`` is the first consumer,
called from BOTH ``on_mount`` (once ``ClientTransport.state_ready()``
resolves — a NEW, separate primitive from ``frames()``, see that method's
own docstring on ``ClientTransport`` for why frame arrival cannot be the
gate: a session with genuinely nothing else happening yields ZERO frames
from ``AgUiTransport.frames()``, ever, confirmed by direct reproduction
before this fix, not guessed) and ``_handle_session_attached_event`` (a
session switch to an agent that already has a pending intervention —
gated on the EXISTING ``_session_switch_generation`` supersede guard
instead, never re-awaiting ``state_ready()``; see that method's own
docstring for why).

Owner escalated the accept criterion mid-implementation twice
(issuecomment-5377475482, -5377481917): "iv 選択肢については local と同じ
UI/UX を期待してる" — a free-text fallback (the Composer's ``Input``,
exactly what owner's own report showed as "Type your answer") satisfies a
weaker "something got presented" witness without satisfying this. Three
required witnesses now (lead-coder's own #5050③ ruling):

- **①** (kept from the issue's original form, deliberately crossing the
  frontend layer — architect: "layer-crossing is intentional here, not
  crossing lets 'rendered but the answer doesn't land' pass"): a remote
  client selecting `[A]lways` for a real permission intervention actually
  persists to `.reyn/approvals.yaml` — not just that the panel drew
  something.
- **②** (the one directly reproducing owner's own symptom, and the one
  that would have caught the FIRST (broken) mount-time design — reading
  ``intervention_head()`` before the frame pump had decoded anything):
  a reconnect whose SSE carries ONLY a STATE_SNAPSHOT and never yields a
  single Frame from ``transport.frames()`` still gets the pending
  intervention presented — the real ``InterventionPanel``, not the
  Composer's free-text input — with real choices.
- **③** (architect's own follow-up finding: the mount-time fix alone does
  not cover this): a session SWITCH to an agent that already has a
  pending intervention also presents it, with local-parity choices — an
  unanswered intervention leaves no history trace (#5047), so nothing on
  the ordinary reset-and-rehydrate switch path would present it otherwise.

Real ``Session`` + real ``PermissionResolver`` + real ``AgentRegistry`` +
real ``AgUiEmitter`` + real ``AgUiTransport`` + a real mounted
``TextualChatApp`` — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from textual_flowview import FlowView

from reyn.core.events.events import Event
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.agent_session import make_session


async def _sse_lines_then_hang(text: str):
    """Yields ``text``'s own lines then blocks forever on a real
    never-resolving primitive (an ``asyncio.Event`` nothing ever
    ``.set()``s), matching a REAL open AG-UI connection with nothing
    further to report yet.

    #5050 ③, measured: a genuinely EXHAUSTED SSE source (a plain
    line-by-line generator over a FIXED string, then done) makes
    ``AgUiTransport.frames()`` raise ``StopAsyncIteration`` on its very
    first ``__anext__()`` when there is nothing to yield — which makes
    ``_pump_frames``'s ``async for`` loop complete with ZERO iterations
    and fall straight into its own ``finally: self.exit()`` (a genuine
    "the connection is over, quit the app" — correct for an actually-
    closed stream, wrong for THIS scenario). A real long-lived connection
    never exhausts this way; it just has nothing more to send yet. A
    finite generator here would tear the WHOLE APP down via that
    ``finally`` before witness ②'s own hydrated-intervention-head worker
    ever gets a chance to run — not a mount race, a test artifact that
    doesn't match the real bug shape."""
    for line in text.split("\n"):
        yield line
    await asyncio.Event().wait()


async def _never_yields():
    """A real, empty async generator — matches the ACTUAL shape #5050's
    own reproduction found: a session with genuinely nothing else
    happening produces no live DisplayFrame at all (``__end__`` is a
    control sentinel the emitter never forwards — see its own docstring)."""
    if False:  # noqa: SIM103 — the idiomatic empty-async-generator shape
        yield


_AGENT = "test-agent"


def _make_session_with_resolver(
    tmp_path: Path,
) -> "tuple[Session, PermissionResolver, AgentRegistry]":
    """A real ``Session`` + a real ``AgentRegistry`` wired to attach IT
    (not a registry-constructed session of its own) — witness ①'s
    ``_snapshot()`` reads several registry accessors beyond
    ``attached_session`` (cost/token/unpriced, keyed by ``attached_name``),
    so a full real registry is cheaper and more honest than hand-rolling
    each of those methods (matching ``test_3338_tui_status_chrome_
    liveness.py``'s own ``holder``-back-reference factory pattern).

    Marks the session attached via ``registry._connection.switch`` directly
    — NOT ``registry.attach()`` — because ``attach()`` unconditionally
    starts the registry-level outbox forwarder AND (unless
    ``start_runner=False``) the full ``session.run()`` router loop; this
    test drives the intervention and drains ``session.outbox`` manually,
    neither of which this scenario needs or wants running underneath it."""
    perm = PermissionResolver({}, project_root=tmp_path, interactive=True)
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            permission_resolver=perm,
            state_log=state_log,
            snapshot_path=tmp_path / f"{profile.name}_snap.json",
            workspace_base_dir=tmp_path,
            registry=holder.get("reg"),
        )

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = registry
    AgentProfile.new(_AGENT, role="").save(tmp_path / ".reyn" / "agents" / _AGENT)
    session = registry.get_or_load(_AGENT)
    registry._connection.switch((_AGENT, _DEFAULT_SID))
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    return session, perm, registry


@pytest.mark.asyncio
async def test_remote_always_choice_persists_to_approvals_yaml(tmp_path, monkeypatch):
    """Tier 2: witness ① — a remote client selecting [A]lways for a real
    permission intervention actually persists the grant to
    ``.reyn/approvals.yaml`` — the layer-crossing witness (architect):
    rendering without the answer landing would pass a narrower test."""
    monkeypatch.chdir(tmp_path)
    session, perm, registry = _make_session_with_resolver(tmp_path)
    decl = PermissionDecl(http_get=[{"host": "*"}])

    # The real permission gate, raising a real UserIntervention with
    # generic_yn_choices() through the SAME bus production uses
    # (session._make_router_intervention_bus()) — no full router/tool
    # dispatch needed; that machinery is not the subject under test here.
    require_task = asyncio.ensure_future(
        perm.require_http_get(
            decl, "news.ycombinator.com",
            session._make_router_intervention_bus(), actor="chat_router",
        )
    )
    announce_msg = await asyncio.wait_for(session.outbox.get(), timeout=2.0)
    assert announce_msg.kind == "intervention"
    assert announce_msg.meta.get("choices")
    assert announce_msg.meta.get("intervention_id"), (
        "the real production announce carries an intervention_id — this "
        "test's own send() correlates the panel's answer through it"
    )

    def session_snapshot() -> dict:
        from reyn.interfaces.repl.status import _snapshot
        return _snapshot(registry) or {}

    # Server side: this connection arrives AFTER the intervention was
    # already raised (#5050③'s own scenario) — the announce frame is not
    # replayed to a fresh connect (#5047's own finding: an unanswered
    # intervention has no history trace); only STATE_SNAPSHOT carries it
    # (#5053). No DisplayFrame at all in this stream.
    emitter = AgUiEmitter(_never_yields(), session_snapshot)

    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def send(payload: dict) -> bool:
        if payload.get("type") == "TOOL_CALL_RESULT":
            return await session.answer_intervention_by_id(
                str(payload.get("toolCallId")),
                choice_id_override=payload.get("choiceId"),
            )
        return False

    transport = AgUiTransport(_sse_lines_then_hang(sse), send)
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, "the pending intervention never got presented"
        assert panel.has_pending() is True

        # generic_yn_choices() order: [y]es, [A]lways, [n]o, [N]ever — the
        # panel pre-highlights the first option (#3299 P2 owner decision),
        # so ONE "down" reaches [A]lways.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

    await asyncio.wait_for(require_task, timeout=2.0)

    approvals_path = tmp_path / ".reyn" / "approvals.yaml"
    assert approvals_path.exists(), "always was never persisted to approvals.yaml"
    saved = yaml.safe_load(approvals_path.read_text(encoding="utf-8")) or {}
    assert saved.get("chat_router/http.get/news.ycombinator.com") is True, saved


@pytest.mark.asyncio
async def test_remote_pending_intervention_presents_even_when_no_frame_ever_arrives(
    tmp_path, monkeypatch,
):
    """Tier 2: witness ② — the reproduction of owner's own symptom, and the
    one that would have caught the FIRST (broken) design: a reconnect
    whose ``AgUiTransport.frames()`` yields ZERO frames (a real,
    reproduced shape — see ``_never_yields``'s own docstring) still
    presents the pending intervention with real choices, because
    ``state_ready()`` — not frame arrival — is what gates the read.

    Strip-falsifier: removing the ``await self._transport.state_ready()``
    in ``_present_hydrated_intervention_head_if_pending`` (reading
    ``intervention_head()`` immediately at mount instead) turns this red
    — ``RemoteReadModel.intervention_head()`` reads
    ``transport.status.values``, which is still empty at that instant
    (measured directly, #5050's own reproduction script) — the panel
    would never open."""
    monkeypatch.chdir(tmp_path)

    state = {
        "attached_name": "test-agent", "model": "opus",
        "pending_intervention_head": {
            "id": "iv-standalone",
            "prompt": "Allow fetching from 'docs.example.com'?",
            "detail": None,
            "choices": [
                {"id": "yes", "label": "[y]es", "hotkey": "y"},
                {"id": "always", "label": "[A]lways", "hotkey": "A"},
                {"id": "no", "label": "[n]o", "hotkey": "n"},
                {"id": "never", "label": "[N]ever", "hotkey": "N"},
            ],
        },
    }

    emitter = AgUiEmitter(_never_yields(), lambda: dict(state))
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines_then_hang(sse), _noop_send)
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(10):
            await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, (
            "a pending intervention behind a frame-less STATE_SNAPSHOT "
            "reconnect was never presented"
        )
        assert panel.has_pending() is True
        # ★owner-required local parity (issuecomment-5377475482): the real
        # choice set (a RadioSet), NOT the Composer's free-text Input —
        # owner's own reported symptom was "Type your answer" appearing
        # instead of the choices this intervention actually carries.
        assert panel.query("RadioSet"), (
            "the hydrated intervention rendered as a free-text prompt, "
            "not the local-parity choice set"
        )

        # A real flow entry rendered too (the presenter side, not just the
        # interactive panel) — no chip options on it (#3299 P1 contract).
        entries = [
            e for e in app.query_one(FlowView).entries if e.item.kind == "intervention"
        ]
        assert entries, "the hydrated intervention never reached the flow view"
        assert any(
            "Allow fetching from 'docs.example.com'?" in e.item.text for e in entries
        )


@pytest.mark.asyncio
async def test_remote_switch_to_agent_with_pending_intervention_presents_it(
    tmp_path, monkeypatch,
):
    """Tier 2: witness ③ — architect's own follow-up finding: witness ②'s
    mount-time fix does NOT cover a session SWITCH to an agent that already
    has a pending intervention. ``_handle_session_attached_event`` resets
    every per-session client state and rehydrates from history — but an
    unanswered intervention leaves no history trace (#5047's own finding,
    ``restore.py``), so nothing on that path would ever present it without
    this PR's shared ``_present_hydrated_intervention_head_if_pending``
    call, gated on the SAME ``_session_switch_generation`` supersede guard
    ``_handle_session_attached_event`` already uses (architect ruling: no
    new generation mechanism) rather than re-awaiting ``state_ready()``
    (the SAME ``AgUiTransport`` persists across a switch — its one-shot
    Event, already set by the FIRST session's snapshot, would resolve
    immediately and lie about the NEW session's own readiness).

    Drives the real ``_handle_session_attached_event`` handler directly
    with a real ``Event`` (matching ``test_4983_session_switch_off_thread
    .py``'s own established idiom for testing this handler in isolation)
    — the connection's initial STATE_SNAPSHOT carries no pending
    intervention; the switch's own (simulated) reconnect burst updates
    ``transport.status`` with the NEW session's pending intervention
    BEFORE the handler runs, matching production ordering (the handler
    only runs in response to a ``session_attached`` frame the SAME
    ``_pump_frames`` loop has already processed, by which point any
    co-delivered STATE_* update is already applied).

    Strip-falsifier: commenting out this PR's ``run_worker(self.
    _present_hydrated_intervention_head_if_pending(switch_generation=...))``
    call in ``_handle_session_attached_event`` turns this red — the panel
    never opens for the switched-to agent's pending intervention."""
    monkeypatch.chdir(tmp_path)

    initial_state = {"attached_name": "agent-a", "model": "opus"}
    emitter = AgUiEmitter(_never_yields(), lambda: dict(initial_state))
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines_then_hang(sse), _noop_send)
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(10):
            await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is False, (
            "sanity: agent-a's own snapshot carries no pending intervention"
        )

        # Simulate the switch's own reconnect burst: agent-b's fresh
        # STATE_SNAPSHOT (carrying its pending intervention) landing on
        # this SAME transport instance BEFORE the session_attached handler
        # runs — matching production ordering (see this test's own
        # docstring).
        transport.status.apply_snapshot({
            "attached_name": "agent-b", "model": "opus",
            "pending_intervention_head": {
                "id": "iv-agent-b",
                "prompt": "Allow fetching from 'switched.example.com'?",
                "detail": None,
                "choices": [
                    {"id": "yes", "label": "[y]es", "hotkey": "y"},
                    {"id": "always", "label": "[A]lways", "hotkey": "A"},
                    {"id": "no", "label": "[n]o", "hotkey": "n"},
                    {"id": "never", "label": "[N]ever", "hotkey": "N"},
                ],
            },
        })

        await app._handle_session_attached_event(
            Event(type="session_attached", data={"agent": "agent-b", "session_id": None})
        )
        for _ in range(10):
            await pilot.pause()

        assert panel.display is True, (
            "a switch to an agent with a pending intervention was never presented"
        )
        assert panel.has_pending() is True
        # ★owner-required local parity: the real InterventionPanel's choice
        # set (a RadioSet), not the Composer's free-text Input — owner's own
        # reported symptom on the mount-time case was "Type your answer"
        # appearing instead.
        assert panel.query("RadioSet"), (
            "the switched-to intervention rendered as a free-text prompt, "
            "not the local-parity choice set"
        )
