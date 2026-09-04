"""Tier 2: #5736 — a remote (AG-UI) client learns, IMMEDIATELY, that an
UNATTACHED session's ``turn_active``/``iv_waiting`` changed — the remote
counterpart to #5734's local wiring. owner's own request (verbatim): "各
agent の状態を reactive で表示してほしい…local/remote 両方対応してね."
Owner ruling B (2026-09-04, verbatim "B だよ"): a remote client MAY see the
status of a same-process session it has not attached to — this PR is about
HOW that reaches the wire promptly, not whether it is allowed.

Values were ALREADY correct before this PR (``AgentRegistry.all_sessions_
status()`` is computed fresh, and ``project_status`` folds it into the SAME
projection both local and remote read) — the gap was purely a wake channel:
nothing told an open AG-UI SSE stream to re-check status when the change
belonged to a session OTHER than the one its own frame source follows.

Architect's confirmed design (issuecomment-5533964539), 6 points this file's
own test names map onto 1:1:
  ① _SessionFrameSource stays bound to exactly ONE session (unchanged) —
     status rides a SEPARATE, connection-scoped _StatusFrameSource.
  ② the same ordered queue/stream, a different frame kind (StatusPingFrame)
     — never a second transport connection.
  ③ subscription lifetime is the CONNECTION, never the attached session —
     a /session switch must not re-subscribe/unsubscribe.
  ④ coalesced per key (here: at most one pending ping at all, since the
     wire payload is never per-key — see StatusPingFrame's own docstring).
  ⑤ snapshot + delta share the SAME visibility function (this PR adds NO
     new one — both already read AgentRegistry.all_sessions_status()).
  ⑥ remove_status_listener is called on disconnect (real gap on origin/main
     before #5734 merged; #5734 landed the removal method itself).

Real AgentRegistry/Session/InterventionRegistry throughout — the end-to-end
tests drive a real _SessionFrameSource/_StatusFrameSource/AgUiEmitter chain
(mirrors test_4534_pr2b_switch_follow_e2e.py's own Frame-level idiom for
exercising the real wire mechanism directly, without a full HTTP server).
The intervention dispatch/answer cycle is the SAME
cheap, real, LLM-free status-transition drive test_5729_status_registry_
wiring.py already established. No mocks anywhere in this file.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    StatusPingFrame,
    _SessionFrameSource,
    _StatusFrameSource,
)
from reyn.interfaces.transport.agui.protocol import parse_sse_blocks
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _two_agent_registry(tmp_path: Path, monkeypatch) -> AgentRegistry:
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create("alpha")
    reg.create("beta")
    return reg



async def _dispatch_intervention(session: Session) -> "tuple[UserIntervention, asyncio.Task]":
    """The SAME cheap, real, LLM-free ``iv_waiting`` transition
    test_5729_status_registry_wiring.py already established."""
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    task = asyncio.ensure_future(session.interventions.dispatch(iv))
    await asyncio.sleep(0)
    return iv, task


# ---------------------------------------------------------------------------
# Unit level — real AgentRegistry, no HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_source_pushes_a_ping_for_an_unattached_sessions_change(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ①②'s own direct witness — a frame source bound to session
    "alpha" observes a real status change on the UNRELATED session "beta"
    (never attached to this connection) as a StatusPingFrame on its own
    ordered queue — the SAME queue frames() already drains, never a second
    stream."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    session_beta = reg.get_or_load("beta")

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    try:
        frames_iter = source.frames()

        iv, task = await _dispatch_intervention(session_beta)
        await asyncio.sleep(0)

        frame = await asyncio.wait_for(frames_iter.__anext__(), timeout=5)
        assert isinstance(frame, StatusPingFrame), (
            f"expected a StatusPingFrame off session alpha's own frame "
            f"source after session beta's (unattached) status changed — "
            f"got {frame!r}"
        )

        await session_beta.interventions.deliver_answer(iv, "ok")
        await task
    finally:
        status_source.close()
        source.close()


@pytest.mark.asyncio
async def test_multiple_changes_before_drain_coalesce_to_fewer_pings(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ★④ — N real status transitions on the SAME unattached
    session, none drained in between, must not produce N separate pings —
    ``push_status_ping``'s own coalescing collapses them (the eventual
    drain always re-reads the CURRENT, whole status, so nothing is lost)."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    session_beta = reg.get_or_load("beta")

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    try:
        transitions = 5
        for _ in range(transitions):
            iv, task = await _dispatch_intervention(session_beta)
            await asyncio.sleep(0)
            await session_beta.interventions.deliver_answer(iv, "ok")
            await task

        # Terminate the collection deterministically: push a real __end__
        # DisplayFrame after the (coalesced) pings, and collect until it.
        source._q.put_nowait(DisplayFrame(OutboxMessage(kind="__end__", text="")))

        pings = 0
        async for frame in source.frames():
            if isinstance(frame, StatusPingFrame):
                pings += 1
            if isinstance(frame, DisplayFrame) and frame.message.kind == "__end__":
                break

        assert pings < transitions, (
            f"{transitions} real status transitions produced {pings} "
            f"pings — expected coalescing to keep this below the "
            f"transition count"
        )
        assert pings >= 1, "at least one ping must still have been produced"
    finally:
        status_source.close()
        source.close()


@pytest.mark.asyncio
async def test_close_unsubscribes_the_status_listener(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: ★⑥ — the disconnect-time teardown witness. Public
    ``status_listener_count()`` (added alongside ``remove_status_listener``
    in #5734) proves the subscription is actually gone, not merely that
    ``close()`` ran without raising."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    before = reg.status_listener_count()

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    status_source.start()
    assert reg.status_listener_count() == before + 1

    status_source.close()
    assert reg.status_listener_count() == before
    source.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: falsify pair — the construction-window try/except in
    ``agui_events`` can race a normal ``close()`` in ``gen()``'s own
    ``finally``; a second ``close()`` must not double-decrement (or raise)."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    before = reg.status_listener_count()

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    status_source.start()
    status_source.close()
    status_source.close()  # must not raise or go negative
    assert reg.status_listener_count() == before
    source.close()


@pytest.mark.asyncio
async def test_two_connect_disconnect_cycles_never_leak_a_listener(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ★⑥'s own explicit acceptance phrasing — "接続→切断を2回して
    listener 数が増えない" — 2 full start/close cycles must return to the
    SAME baseline, not accumulate."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    before = reg.status_listener_count()

    for _ in range(2):
        source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
        status_source = _StatusFrameSource(reg, sink=source)
        status_source.start()
        status_source.close()
        source.close()

    assert reg.status_listener_count() == before


@pytest.mark.asyncio
async def test_session_switch_does_not_resubscribe_the_status_listener(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ★③ — a real ``registry.attach_session`` switch-follow (the
    SAME mechanism ``_SessionFrameSource`` reacts to via
    ``add_attach_listener``) must leave the status subscription COUNT
    unchanged, twice, proving lifetime tracks the CONNECTION, never the
    currently-attached session."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    try:
        after_start = reg.status_listener_count()

        await reg.attach_session("alpha", sid_b)
        await asyncio.sleep(0)
        assert reg.status_listener_count() == after_start, (
            "a same-agent session switch must not touch the status "
            "subscription count"
        )

        await reg.attach_session("alpha", "main")
        await asyncio.sleep(0)
        assert reg.status_listener_count() == after_start, (
            "switching back must not touch it either"
        )
    finally:
        status_source.close()
        source.close()


# ---------------------------------------------------------------------------
# Emitter level — a real AgUiEmitter, no HTTP
# ---------------------------------------------------------------------------


async def _one_shot_frames(*items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_emitter_emits_state_delta_when_a_status_ping_arrives() -> None:
    """Tier 2: ★①②⑤ at the emitter level — a StatusPingFrame (never
    encoded as a DisplayFrame/EventFrame) causes the SAME
    ``StatusModel.delta``/``encode_state_delta`` path every other status
    change already uses to fire, with the CURRENT ``status_provider()``
    value (never a payload carried BY the ping itself, since it carries
    none — see that class's own docstring)."""
    calls = [0]

    def _status_provider():
        calls[0] += 1
        # First call (the connect-time reconnect snapshot) sees an empty
        # roster; the second (triggered by the ping) sees beta.
        if calls[0] == 1:
            return {"all_sessions_status": []}
        return {
            "all_sessions_status": [
                {"agent": "beta", "sid": "main", "turn_active": False, "iv_waiting": True},
            ],
        }

    emitter = AgUiEmitter(_one_shot_frames(StatusPingFrame()), _status_provider)
    chunks = [chunk async for chunk in emitter.stream()]
    sse_text = "".join(chunks)
    events = parse_sse_blocks(sse_text.split("\n"))

    deltas = [ev for ev in events if ev.type == "STATE_DELTA"]
    assert deltas, f"expected a STATE_DELTA after the status ping — events: {events!r}"
    assert deltas[0].data["delta"]["all_sessions_status"] == [
        {"agent": "beta", "sid": "main", "turn_active": False, "iv_waiting": True},
    ]


@pytest.mark.asyncio
async def test_emitter_emits_nothing_for_a_redundant_status_ping() -> None:
    """Tier 2: falsify pair (deny side) — a ping that arrives after the
    change it was for was ALREADY picked up (e.g. by an ordinary frame's
    own per-frame delta check) must not manufacture a second, empty
    STATE_DELTA."""
    def _status_provider():
        return {"all_sessions_status": []}

    emitter = AgUiEmitter(_one_shot_frames(StatusPingFrame()), _status_provider)
    chunks = [chunk async for chunk in emitter.stream()]
    sse_text = "".join(chunks)
    events = parse_sse_blocks(sse_text.split("\n"))

    deltas = [ev for ev in events if ev.type == "STATE_DELTA"]
    assert deltas == []


# ---------------------------------------------------------------------------
# ★ End-to-end — a real _SessionFrameSource/_StatusFrameSource/AgUiEmitter
# chain (mirrors test_4534_pr2b_switch_follow_e2e.py's own Frame-level
# idiom for exercising the real wire mechanism without a full HTTP server;
# owner ruling B's own witness: an UNATTACHED session's change reaches the
# wire immediately)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_client_sees_an_unattached_sessions_change_without_a_frame_of_its_own(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ★ the central, owner-facing witness. Connects a real
    ``_SessionFrameSource``/``_StatusFrameSource`` pair bound to agent
    "alpha" (mirrors what ``agui_events`` wires per-connection), then
    drives a REAL ``iv_waiting`` transition on agent "beta" — a session
    this connection never attached to and which produces NO frame on
    alpha's own outbox/audit-event stream. The change must still reach
    this stream's own encoded SSE text as a STATE_DELTA."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_alpha = await reg.attach("alpha")
    session_beta = reg.get_or_load("beta")

    source = _SessionFrameSource(session_alpha, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    # The REAL status_provider production wiring uses (endpoint.py's own
    # ``_status_provider`` closure) — reads `source.current_session()`
    # fresh, never a frozen/constant value, exactly what a real connection
    # does.
    from reyn.interfaces.repl.status import _snapshot_for_session

    emitter = AgUiEmitter(
        source.frames(), lambda: _snapshot_for_session(reg, source.current_session()),
    )

    stream_iter = emitter.stream()
    try:
        # Prime past the connect-time reconnect snapshot chunks
        # (MESSAGES_SNAPSHOT then STATE_SNAPSHOT — 2 chunks, same as
        # test_4534_pr2b's own "prime past the initial connect snapshot").
        await stream_iter.__anext__()
        await stream_iter.__anext__()

        iv, task = await _dispatch_intervention(session_beta)
        try:
            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=5)
        finally:
            await session_beta.interventions.deliver_answer(iv, "ok")
            await task
    finally:
        status_source.close()
        source.close()

    events = parse_sse_blocks(chunk.split("\n"))
    deltas = [ev for ev in events if ev.type == "STATE_DELTA"]
    assert deltas, (
        f"session alpha's own connection never saw beta's (unattached) "
        f"status change reach the wire: {chunk!r}"
    )
    beta_rows = [
        row for row in deltas[0].data["delta"].get("all_sessions_status", [])
        if row.get("agent") == "beta"
    ]
    assert beta_rows and beta_rows[0]["iv_waiting"] is True, (
        f"the delta did not carry beta's real iv_waiting=True transition: "
        f"{deltas[0].data!r}"
    )


# ---------------------------------------------------------------------------
# Client side — RemoteReadModel.add_status_listener's own real wiring
# (AgUiTransport decoding a real server-produced STATE_DELTA), mirroring
# test_agui_state_read_model.py's own "real server text → real client"
# idiom.
# ---------------------------------------------------------------------------


async def _sse_lines(text: str):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_remote_read_model_status_listener_fires_on_a_real_wire_delta() -> None:
    """Tier 2: the CLIENT half of this PR — was the base class's no-op
    default (the exact gap #5736 disclosed: values already correct over
    the wire, nothing woke the ONE consumer). A REAL server-produced
    STATE_DELTA (via a real ``AgUiEmitter``), decoded by a REAL
    ``AgUiTransport``, must reach a callback registered through
    ``RemoteReadModel.add_status_listener`` — the exact seam
    ``TextualChatApp.on_mount`` calls, unchanged, for either read model."""
    state = {"all_sessions_status": []}

    def status_provider():
        return dict(state)

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="status", text="x"))
        state["all_sessions_status"] = [
            {"agent": "beta", "sid": "main", "turn_active": False, "iv_waiting": True},
        ]
        yield DisplayFrame(OutboxMessage(kind="status", text="y"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_DELTA" in sse, "test setup sanity: the server must have emitted a delta"

    async def _send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _send)
    read_model = RemoteReadModel(transport)
    calls: "list[tuple]" = []
    read_model.add_status_listener(lambda *args: calls.append(args))

    async for _frame in transport.frames():
        pass

    assert calls == [("beta", "main", False, True, 1)], (
        f"expected exactly one callback carrying beta's real transition — "
        f"got {calls!r}"
    )


@pytest.mark.asyncio
async def test_remote_read_model_status_listener_ignores_unrelated_deltas() -> None:
    """Tier 2: falsify pair — a STATE_DELTA touching some OTHER key (cost,
    ctx, …) must not spuriously fire the listener; only a delta that
    actually carries ``all_sessions_status`` may."""
    state = {"all_sessions_status": [], "cost_agent": 1.0}

    def status_provider():
        return dict(state)

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="status", text="x"))
        state["cost_agent"] = 5.0  # unrelated key changes
        yield DisplayFrame(OutboxMessage(kind="status", text="y"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_DELTA" in sse

    async def _send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _send)
    read_model = RemoteReadModel(transport)
    calls: "list[tuple]" = []
    read_model.add_status_listener(lambda *args: calls.append(args))

    async for _frame in transport.frames():
        pass

    assert calls == []


@pytest.mark.asyncio
async def test_remote_read_model_remove_status_listener_stops_further_calls() -> None:
    """Tier 2: the REMOTE side of ★⑥ — ``remove_status_listener`` (called
    from ``TextualChatApp.on_unmount``, unchanged) must genuinely stop
    delivery, not merely accept the call."""
    state = {"all_sessions_status": []}

    def status_provider():
        return dict(state)

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="status", text="x"))
        state["all_sessions_status"] = [
            {"agent": "beta", "sid": "main", "turn_active": True, "iv_waiting": False},
        ]
        yield DisplayFrame(OutboxMessage(kind="status", text="y"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _send)
    read_model = RemoteReadModel(transport)
    calls: "list[tuple]" = []
    callback = lambda *args: calls.append(args)  # noqa: E731
    read_model.add_status_listener(callback)
    read_model.remove_status_listener(callback)

    async for _frame in transport.frames():
        pass

    assert calls == []
