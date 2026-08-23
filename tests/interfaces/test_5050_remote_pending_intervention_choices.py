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
gated on the EXISTING ``_session_switch_generation`` supersede guard,
AND re-awaiting ``state_ready()`` too — corrected mid-review, architect
co-vet issuecomment-5377613210, from this PR's own first draft, which
skipped that re-await on the mistaken theory that a switch's state was
"already applied" by the time the handler runs; see that method's own
docstring for the real ordering and why the fix is per-episode
``state_ready()`` clearing, not "await nothing").

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
from textual_flowview import FlowView

from reyn.core.events.events import Event
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    encode_frame,
    encode_state_snapshot,
    to_sse,
)
from reyn.interfaces.transport.frames import EventFrame
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
    doesn't match the real bug shape.

    A real ``asyncio.sleep(0)`` BETWEEN every yielded line (not merely
    after the whole block) — without it, ``_pump_frames``'s own worker
    (scheduled first in ``on_mount``) decodes the entire single-block SSE
    text with NO intervening real suspension, so it reaches ``_state_
    ready_event.set()`` before the hydrated-intervention-head worker
    (scheduled second) ever gets a first turn — making ``state_ready()``'s
    own await a no-op in THIS construction and any strip of it silently
    pass (measured: 3/3 stripped runs passed regardless). The per-line
    yield gives the second worker a genuine, non-hypothetical chance to
    read ``intervention_head()`` before the block finishes decoding,
    which is what makes the strip-falsifier below actually load-bearing."""
    for line in text.split("\n"):
        yield line
        await asyncio.sleep(0)
    await asyncio.Event().wait()


async def _never_yields():
    """A real, empty async generator — matches the ACTUAL shape #5050's
    own reproduction found: a session with genuinely nothing else
    happening produces no live DisplayFrame at all (``__end__`` is a
    control sentinel the emitter never forwards — see its own docstring)."""
    if False:  # noqa: SIM103 — the idiomatic empty-async-generator shape
        yield


async def _wait_until(pilot, condition) -> None:
    """Poll ``pilot.pause()`` unboundedly until ``condition()`` is true —
    CLAUDE.md's Ceiling rule (no ``range(N)`` wrapping a wait; wait on the
    condition unboundedly, CI's own ``--timeout`` is the kill switch), not
    a fixed pause count that could pass on luck or silently under-wait."""
    while not condition():
        await pilot.pause()


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
async def test_remote_always_choice_persists_to_the_approval_ledger(tmp_path, monkeypatch):
    """Tier 2: witness ① — the COMBINED witness lead-coder's own #5050③
    block found missing (no single test looked at the whole SET owner
    required): a construction where ``transport.frames()`` yields ZERO
    frames (the exact reconnect-after-the-fact scenario ② also covers)
    where (a) the real ``InterventionPanel`` appears — not the Composer's
    free-text input, (b) its choice set matches local's (``[A]lways``
    selectable), and (c) selecting it actually persists a grant to
    ``.reyn/approvals.yaml`` — the layer-crossing half (architect):
    rendering without the answer landing would pass a narrower test.

    No ceiling anywhere below (CLAUDE.md Ceiling rule): every wait is on
    a real condition (:func:`_wait_until`, or a bare ``await`` on a
    coroutine/queue this test's own setup guarantees resolves)."""
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
    announce_msg = await session.outbox.get()
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
    # (#5053). No DisplayFrame at all in this stream — the SAME
    # frame-free construction as witness ②.
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
        panel = app.query_one(InterventionPanel)
        await _wait_until(pilot, lambda: panel.display is True)
        assert panel.has_pending() is True
        # ★owner-required local parity (issuecomment-5377475482): the real
        # choice set (a RadioSet), NOT the Composer's free-text Input.
        radio_set = panel.query_one("RadioSet")
        assert radio_set, (
            "the hydrated intervention rendered as a free-text prompt, "
            "not the local-parity choice set"
        )
        # The panel's own auto-focus (``on_tabbed_content_tab_activated``)
        # is deferred via ``call_after_refresh`` — ``panel.display`` becomes
        # True a full refresh cycle BEFORE focus actually lands on the
        # RadioSet. Waiting on the REAL condition (has_focus), not an
        # arbitrary extra pause, avoids pressing keys before Textual is
        # ready to route them to this widget.
        await _wait_until(pilot, lambda: radio_set.has_focus)

        # generic_yn_choices() order: [y]es, [A]lways, [n]o, [N]ever — the
        # panel pre-highlights the first option (#3299 P2 owner decision),
        # so ONE "down" reaches [A]lways.
        await pilot.press("down")
        await pilot.press("enter")
        # #5153: persistence moved from a snapshot approvals.yaml to the
        # append-only approvals.jsonl ledger — wait on THAT file existing.
        ledger_path = tmp_path / ".reyn" / "approvals.jsonl"
        await _wait_until(pilot, lambda: ledger_path.exists())

    await require_task

    from reyn.security.permissions.approval_ledger import ApprovalLedger
    saved, _bound = ApprovalLedger(ledger_path).fold()
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
        panel = app.query_one(InterventionPanel)
        await _wait_until(pilot, lambda: panel.display is True)
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


async def _sse_burst_then_switch_then_hang(
    initial_sse: str, switch_sse: str, release: "asyncio.Event",
):
    """Yield ``initial_sse``'s lines, then BLOCK (a real, unbounded
    ``asyncio.Event().wait()`` — nothing this test's own setup resolves
    until the test itself calls ``release.set()``) until ``release`` is
    set, then yield ``switch_sse``'s lines, then hang forever (matching
    :func:`_sse_lines_then_hang`'s own rationale: a real connection never
    exhausts ``frames()`` this way).

    #5050③ co-vet correction (architect, issuecomment-5377613210): the
    FIRST draft of witness ③ simulated the switch by calling
    ``transport.status.apply_snapshot(...)`` directly, BEFORE invoking
    ``_handle_session_attached_event`` — backwards. ``AgUiEmitter``'s own
    reconnect-protocol barrier (``emitter.py``'s module docstring: "the
    re-fire happens strictly AFTER the ``session_attached`` frame is
    forwarded — never before") means production has the switched-to
    session's STATE_SNAPSHOT arrive AFTER the ``session_attached`` frame,
    not before. This generator reproduces that real ordering: ``switch_
    sse`` below is built as a session_attached EventFrame followed by the
    NEW session's own STATE_SNAPSHOT, and the app's REAL ``_pump_frames``
    loop (not a hand-invoked handler call) decodes both in that order —
    so the fix under test (``AgUiTransport`` clearing ``state_ready()``'s
    Event on ``session_attached`` decode, setting it again on the NEXT
    STATE_SNAPSHOT) is exercised exactly as production hits it.

    A real ``asyncio.sleep(0)`` between every yielded line — same
    reasoning as :func:`_sse_lines_then_hang`'s own docstring: without a
    genuine per-line suspension, a worker scheduled after ``_pump_frames``
    never gets a real chance to observe an in-between state, silently
    weakening any strip-falsifier that depends on that window existing."""
    for line in initial_sse.split("\n"):
        yield line
        await asyncio.sleep(0)
    await release.wait()
    for line in switch_sse.split("\n"):
        yield line
        await asyncio.sleep(0)
    await asyncio.Event().wait()


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
    call.

    Drives the switch through the REAL ``_pump_frames`` pipeline — a real
    ``session_attached`` ``EventFrame`` followed by the NEW session's own
    STATE_SNAPSHOT, encoded and decoded through the actual wire codec
    (``encode_frame``/``encode_state_snapshot``/``to_sse``), in PRODUCTION
    ORDER (session_attached strictly before the fresh snapshot — see
    :func:`_sse_burst_then_switch_then_hang`'s own docstring for the
    ordering bug this corrects over this PR's first draft) — never a
    hand-invoked ``_handle_session_attached_event`` call, so the handler
    fires exactly when and how production fires it.

    Strip-falsifier: commenting out this PR's ``run_worker(self.
    _present_hydrated_intervention_head_if_pending(switch_generation=...))``
    call in ``_handle_session_attached_event`` turns this red — the panel
    never opens for the switched-to agent's pending intervention."""
    monkeypatch.chdir(tmp_path)

    initial_state = {"attached_name": "agent-a", "model": "opus"}
    emitter = AgUiEmitter(_never_yields(), lambda: dict(initial_state))
    initial_sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in initial_sse

    agent_b_state = {
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
    }
    # The real wire encoding, PRODUCTION ORDER: session_attached EventFrame
    # first, agent-b's own fresh STATE_SNAPSHOT strictly after (matches
    # ``_SessionFrameSource``/``AgUiEmitter``'s real reconnect barrier).
    switch_sse = to_sse(
        encode_frame(
            EventFrame(Event(type="session_attached", data={"agent": "agent-b", "session_id": None}))
        )
    ) + to_sse(encode_state_snapshot(agent_b_state))

    release = asyncio.Event()

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(
        _sse_burst_then_switch_then_hang(initial_sse, switch_sse, release), _noop_send,
    )
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(InterventionPanel)
        release.set()
        await _wait_until(pilot, lambda: panel.display is True)
        assert panel.has_pending() is True
        # ★owner-required local parity: the real InterventionPanel's choice
        # set (a RadioSet), not the Composer's free-text Input — owner's own
        # reported symptom on the mount-time case was "Type your answer"
        # appearing instead.
        assert panel.query("RadioSet"), (
            "the switched-to intervention rendered as a free-text prompt, "
            "not the local-parity choice set"
        )
