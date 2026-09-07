"""Tier 2: #5885 — the shrink-flow progress state reaches a remote (AG-UI)
client, and reaches it WITHOUT a display frame.

Owner-hit (web/connect, reyn-self): "compact 始まったけど tui 表示がスピナーも
なく進捗も不明 … compact 完了通知もなく". Two structural holes (architect):

① ``project_status`` carried no compaction key, ``project_remote_snapshot``
   hard-coded ``compaction_progress_raw: None`` and the remote capability
   said ``compaction_progress_reported=False`` — so ``_refresh_compaction_
   progress`` returned early on every remote client, by design.
② The emitter projects a STATE_DELTA only after a display frame or a status
   ping, and compaction produces no display frame of its own while it runs
   — so even with ① fixed, a remote would learn nothing until unrelated
   traffic happened to flow.

Ruling 1 (①): the state rides the wire like ``halted_reason`` does.
Ruling 2 (②): the compaction audit-events join ``AgentRegistry``'s status-
listener kinds, so each one pings the connection and a delta follows when
the projection changed; ``compaction_episode_ended`` (new, emitted AFTER
the flag/depth reset on both paths) is the settle.

Everything here is real: a real ``AgUiEmitter`` producing real SSE text, a
real ``AgUiTransport`` decoding it, a real ``TextualChatApp`` over a real
``RemoteReadModel``; on the server side a real ``AgentRegistry``/``Session``
with the real ``_SessionFrameSource``/``_StatusFrameSource`` pair (the
``test_5736_remote_status_reactive.py`` harness). The frame-less witness
drives a REAL ``/compact`` (one stub turn for history, then the client-side
slash layer) so ``is_compacting`` genuinely rises and falls — an earlier
form of that test drove ``recovery_summary_persisted`` on the audit bus and
stayed green under its own strip, because that event now has a forwarder
row (ruling 3) and the ROW's display frame carried the delta: a second
path to the observable, not the ping.

Strips (verified): dropping ``compaction_progress_raw`` from ``project_
status`` → the client witness finds no entry; dropping the compaction kinds
from ``_STATUS_AUDIT_EVENT_KINDS`` → the settle witness's last delta reads a
stale ``is_compacting=True`` and the ping witness gets the ``__end__`` frame
instead of a ping.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    StatusPingFrame,
    _SessionFrameSource,
    _StatusFrameSource,
)
from reyn.interfaces.transport.agui.protocol import parse_sse_blocks
from reyn.interfaces.transport.agui.state import project_status
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def test_project_status_carries_the_progress_state_verbatim() -> None:
    """Tier 1: ruling 1's wire half — the key rides ``project_status`` as
    the plain dict ``Session.compaction_progress_raw()`` produced; ``None``
    when the snapshot has none (never a fabricated ``{"is_compacting": False}``)."""
    raw = {"is_compacting": True, "raw_middle_remaining": 5, "raw_middle_total": 20}
    assert project_status({"compaction_progress_raw": raw})["compaction_progress_raw"] == raw
    assert project_status({})["compaction_progress_raw"] is None


# ── client side: a real wire → a real app ──────────────────────────────────


async def _sse_lines(text: str):
    for line in text.split("\n"):
        yield line


def _progress_entries(app: TextualChatApp) -> list:
    return [e for e in app.conversation if "is_compacting" in (e.item.meta or {})]


@pytest.mark.asyncio
async def test_a_remote_client_raises_and_settles_the_progress_entry_from_deltas() -> None:
    """Tier 2: ruling 1's client half. The server's status toggles
    ``is_compacting`` True then False between two frames; the STATE_DELTAs
    a real emitter produces, decoded by a real transport into a real app
    over ``RemoteReadModel``, must create the shrink-flow entry and then
    settle it — the spinner the owner never saw."""
    state: dict = {"compaction_progress_raw": {"is_compacting": False}}

    def status_provider():
        return dict(state)

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="status", text="x"))
        state["compaction_progress_raw"] = {
            "is_compacting": True, "raw_middle_remaining": 3, "raw_middle_total": 9,
        }
        yield DisplayFrame(OutboxMessage(kind="status", text="y"))
        state["compaction_progress_raw"] = {"is_compacting": False}
        yield DisplayFrame(OutboxMessage(kind="system", text="after-compaction"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert sse.count("STATE_DELTA") >= 2, "setup: the server must have emitted both deltas"

    async def _send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _send)
    transport.start()
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))
    async with app.run_test(size=(100, 30)) as pilot:
        # The system row is the LAST frame before __end__: once it is on
        # the pane, every delta before it has been processed.
        while not any(e.item.text == "after-compaction" for e in app.conversation):
            await pilot.pause()
        entries = _progress_entries(app)
        assert entries, (
            "no shrink-flow entry was created on the remote client — the "
            "STATE_DELTA's compaction_progress_raw never reached the read model"
        )
        assert entries[-1].item.meta.get("is_compacting") is False, (
            f"the entry never settled after is_compacting went False: "
            f"{entries[-1].item.meta!r}"
        )


# ── server side: a real registry, no display frame ─────────────────────────


def _registry(tmp_path: Path, monkeypatch) -> AgentRegistry:
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        from reyn.config import CompactionConfig

        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
            # The ``test_5633_ladder_compact_failure_closes_marker.py`` shape:
            # a small body cap and the chars//4 estimate so a handful of
            # stub turns leaves a compactable MIDDLE behind the protected
            # head (one turn is entirely head-protected: candidate_count 0).
            compaction_config=CompactionConfig(
                body_token_cap=1500, use_chars4_estimate=True, section_caps_spec_tokens=0,
            ),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("alpha")
    return reg


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_the_settle_after_compact_reaches_the_wire_without_a_display_frame(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ruling 2, on the REAL flip. Six stub turns give the session
    a compactable middle; ``/compact`` through the real client-side slash
    layer (the same ``_compact_now_for_op`` binding production uses)
    raises ``CompactionController``'s flag, runs the engine, and resets it.

    Two claims, with different strengths:

    - ORDER (the settle's load-bearing fact; strip-verified by REMOVING
      the ``compaction_episode_ended`` emit — the key is then absent and
      the assert is red): read in the audit subscriber, on the real
      session, ``is_compacting`` is False at ``compaction_episode_ended``.
      Subscriber dispatch is DEFERRED (``EventLog._dispatch_consumer``), so
      this reads the flag at dispatch time, not emit time — which is
      exactly what a status ping's projection reads too. An event emitted
      after the reset is dispatched after it, always; ``compaction_
      completed``/``failed`` are emitted before the reset and their
      dispatch lands before or after it as scheduling falls (measured
      False here on the operator path), so neither is pinned — neither is
      the settle. ``compaction_started`` reads True because the engine's
      LLM await lets the consumer run mid-episode. That the kind pings at
      all is the next test's subject.
    - WIRE (accept side only, NOT a strip witness): after ``/compact`` the
      stream carried at least one delta reading True and its last
      compaction delta reads False. The ``[↑ … compacted]`` forwarder row
      is a display frame whose projection reads True or False depending
      on whether the emitter drains it before or after the reset — a
      scheduling fact — so this assert cannot tell the ping from the row
      and is not claimed to. Disclosed here, per six-questions Q4.

    The stream is terminated with a real ``__end__`` so nothing here waits
    on a hang."""
    # The ``test_5633_ladder_compact_failure_closes_marker.py`` shape: a
    # small model window so the engine's head budget (a weight of T_max)
    # protects a couple of messages and the rest is a compactable middle —
    # with the default window every message of a short session is head.
    import reyn.llm.model_budget as _mb
    from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
    from reyn.interfaces.transport.in_process import InProcessTransport

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 3000)

    reg = _registry(tmp_path, monkeypatch)
    session = await reg.attach("alpha")
    source = _SessionFrameSource(session, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    from reyn.interfaces.repl.status import _snapshot_for_session

    emitter = AgUiEmitter(
        source.frames(), lambda: _snapshot_for_session(reg, source.current_session()),
    )
    chunks: list[str] = []

    async def _collect(stream_iter) -> None:
        # Drained CONCURRENTLY with the drive below — the emitter projects
        # a delta when a frame/ping is pulled, so a stream pulled only
        # after the fact would read every projection post-reset.
        async for chunk in stream_iter:
            chunks.append(chunk)

    collector: "asyncio.Task[None] | None" = None
    try:
        stream_iter = emitter.stream()
        await stream_iter.__anext__()  # MESSAGES_SNAPSHOT
        await stream_iter.__anext__()  # STATE_SNAPSHOT
        collector = asyncio.create_task(_collect(stream_iter))

        settled = asyncio.Event()
        session.subscribe_audit_events(lambda _e: settled.set(), kinds={"turn_settled"})
        # What the compaction machinery itself said — only for the setup
        # message below, so a "never entered an episode" red names WHY.
        compaction_trail: list[tuple[str, dict]] = []
        # ORDER: the live accessor, read AT each audit-event.
        flag_at: dict[str, bool] = {}
        session.subscribe_audit_events(
            lambda e: flag_at.__setitem__(e.type, session.compaction_progress_raw()["is_compacting"]),
            kinds={"compaction_started", "compaction_completed", "compaction_episode_ended"},
        )
        session.subscribe_audit_events(
            lambda e: compaction_trail.append((e.type, dict(e.data or {}))),
            kinds={
                "compaction_check", "compact_op_requested", "compact_op_completed",
                "compact_op_failed", "compact_op_unavailable", "compaction_started",
                "compaction_failed", "compaction_completed", "compaction_episode_ended",
            },
        )
        for n in range(6):
            settled.clear()
            await session.submit_user_text(f"turn {n}: " + "lorem ipsum " * 35)
            await settled.wait()

        handled = await maybe_dispatch_slash(
            InProcessTransport(reg, intervention_channel="test-5885"), "/compact",
        )
        assert handled, "setup: /compact was not dispatched by the slash layer"
        await session._put_outbox(OutboxMessage(kind="__end__", text=""))
        await collector
    finally:
        if collector is not None and not collector.done():
            collector.cancel()
        status_source.close()
        source.close()
        await reg.shutdown()

    events = [ev for c in chunks for ev in parse_sse_blocks(c.split("\n"))]
    progress = [
        (idx, (ev.data.get("delta") or {}).get("compaction_progress_raw"))
        for idx, ev in enumerate(events)
        if ev.type == "STATE_DELTA" and "compaction_progress_raw" in (ev.data.get("delta") or {})
    ]
    assert any((raw or {}).get("is_compacting") is True for _, raw in progress), (
        f"setup: /compact never entered an episode on the wire — no delta read "
        f"is_compacting=True. compaction audit trail: {compaction_trail!r}; "
        f"deltas: {[t.data.get('delta') for t in events if t.type == 'STATE_DELTA']!r}"
    )
    _last_idx, last_raw = progress[-1]
    assert (last_raw or {}).get("is_compacting") is False, (
        "the remote never learned the episode ended — the last "
        f"compaction_progress_raw on the wire still reads {last_raw!r}"
    )
    # ORDER — the strip-verified claim.
    assert flag_at.get("compaction_started") is True, flag_at
    assert flag_at.get("compaction_episode_ended") is False, (
        "compaction_episode_ended never fired (key absent) or fired with the flag "
        "still up — either way no ping projects the settled state and the remote "
        f"never settles: {flag_at!r}"
    )


@pytest.mark.asyncio
async def test_the_episode_end_event_pings_the_connection(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the settle trigger. ``compaction_episode_ended`` — emitted
    after the controller's flag reset / the ladder's depth reset — is a
    status kind: it must land a ``StatusPingFrame`` on the connection's
    own frame queue. (What a ping turns into is 5736's subject.)"""
    reg = _registry(tmp_path, monkeypatch)
    session = await reg.attach("alpha")
    source = _SessionFrameSource(session, registry=reg, agent_name="alpha")
    status_source = _StatusFrameSource(reg, sink=source)
    source.start()
    status_source.start()
    try:
        frames_iter = source.frames()
        session.router_host.events.emit("compaction_episode_ended", path="controller", failed=False)
        await session._put_outbox(OutboxMessage(kind="__end__", text=""))
        frame = await frames_iter.__anext__()
        assert isinstance(frame, StatusPingFrame), (
            f"compaction_episode_ended did not ping the connection — got {frame!r} "
            f"(the __end__ frame means no ping was queued ahead of it)"
        )
    finally:
        status_source.close()
        source.close()
        await reg.shutdown()
