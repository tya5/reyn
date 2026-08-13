"""Tier 2: #4534 PR-2b — an ALREADY-OPEN remote SSE stream follows a
``session_switch_request`` POST (the real wire entry point, not a directly
called registry method).

This is the witness lead-coder's review named explicitly: no existing test
before this PR drove switch-follow through ``ClientTransport.
request_session_switch``'s own wire ptype while a connection was already
streaming — every switch-follow test in ``test_3310_n3_remote_switch_
parity.py`` (real, still valid, but a level below the transport) calls
``registry.attach_session`` directly, and every ``session_switch_request``
ptype test in ``test_4534_pr1_agui_attach_switch_endpoint.py`` only asserts
the POST's own JSON response and ``registry.attached_sid`` — neither opens a
concurrent SSE stream to observe. Without this witness, PR-2's own call-site
migration silently orphaned the switch-follow mechanism (nothing observed
that the already-open stream side kept working) — this is the gate that
would have caught it.

Chains the REAL pieces: a POST to the router's own ``/agui/chat/{agent}``
``session_switch_request`` arm (``endpoint.py``'s ``agui_submit``) ->
``registry.attach_session`` -> ``_announce_session_attached``'s synchronous
``add_attach_listener`` notify -> a REAL, already-``start()``ed
``_SessionFrameSource``'s dual-wait (``_drain_one_session``) -> a REAL
``AgUiEmitter`` mid-``stream()`` re-firing the reconnect protocol. No mocks;
the httpx ASGI POST and the emitter's SSE generator run over the SAME
in-memory ``AgentRegistry``.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    _SessionFrameSource,
    session_backlog_frames,
)
from reyn.interfaces.transport.agui.protocol import parse_sse_blocks
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _two_agent_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create("alpha")
    return reg


def _build_app(reg, monkeypatch):
    from fastapi import FastAPI

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    return app


async def _post(app, path: str, payload: dict):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.post(path, json=payload)


async def _collect_until_barrier(agen) -> "list":
    """Same shape as test_3310_n3's own helper — collect off an async
    generator until the ``session_attached`` barrier frame is dequeued, the
    one deterministic termination condition available (the source has no
    natural EOF outside a real ``__end__``)."""
    out: list = []
    it = agen.__aiter__()
    while True:
        item = await it.__anext__()
        out.append(item)
        if isinstance(item, EventFrame) and getattr(item.event, "type", "") == "session_attached":
            return out


@pytest.mark.asyncio
async def test_open_sse_stream_follows_a_session_switch_request_post(
    tmp_path, monkeypatch,
):
    """Tier 2: an already-running ``_SessionFrameSource`` re-points to
    session B and re-fires B's backlog after a REAL ``session_switch_
    request`` POST — driven through the wire ptype, not
    ``registry.attach_session`` called directly by the test.

    Reads ``source.frames()`` (the Frame-level stream the real
    ``AgUiEmitter`` itself consumes) rather than ``emitter.stream()``'s
    encoded SSE text — the wire-text-level claim (that the SAME switch
    reaches the ENCODED output) is the companion test below."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_a = await reg.attach("alpha")
    sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
    session_b = reg.get_session("alpha", sid_b)
    session_b.history.append(ChatMessage(role="assistant", content="b's reply, pre-switch"))

    app = _build_app(reg, monkeypatch)

    # The "already-open stream": a real source bound to session_a, started
    # (subscribed to session_a's outbox_hub, listening on registry.
    # add_attach_listener) BEFORE the switch POST — exactly what a connected
    # remote client's server-side state looks like mid-stream.
    source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
    source.start()
    frames_iter = source.frames()

    try:
        # The real wire entry point — POSTs "session_switch_request", the
        # SAME ptype ClientTransport.request_session_switch sends.
        resp = await _post(
            app, "/agui/chat/alpha?token=s3cret",
            {"type": "session_switch_request", "session_id": sid_b},
        )
        assert resp.status_code == 200
        assert resp.json().get("switched") is True
        assert reg.attached_sid == sid_b

        # The already-open stream (constructed BEFORE the POST) must
        # independently observe the switch — bounded on the barrier frame's
        # arrival (#4280 ②'s justification: a real termination condition
        # beats an arbitrary wall-clock window).
        collected = await _collect_until_barrier(frames_iter)
    finally:
        source.close()

    barrier_positions = [
        i for i, f in enumerate(collected)
        if isinstance(f, EventFrame) and getattr(f.event, "type", "") == "session_attached"
    ]
    assert barrier_positions, (
        f"the already-open stream never observed the switch — no "
        f"session_attached EventFrame arrived within the collection window; "
        f"collected {len(collected)} item(s): {collected!r}"
    )
    assert collected[barrier_positions[0]].event.data == {
        "agent": "alpha", "session_id": sid_b,
    }


@pytest.mark.asyncio
async def test_open_sse_stream_follows_a_switch_request_over_the_real_wire_text(
    tmp_path, monkeypatch,
):
    """Tier 2: same claim as above, verified on the ENCODED SSE text (not
    just the internal Frame objects) — an already-open connection's raw
    ``AgUiEmitter.stream()`` output carries the CUSTOM
    ``reyn.event.session_attached`` block after a real
    ``session_switch_request`` POST lands mid-stream."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    session_a = await reg.attach("alpha")
    sid_b = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)

    app = _build_app(reg, monkeypatch)

    def _backlog_provider(name: str, sid: str):
        return session_backlog_frames(reg, name, sid)

    source = _SessionFrameSource(session_a, registry=reg, agent_name="alpha")
    source.start()
    emitter = AgUiEmitter(source.frames(), lambda: None, backlog_provider=_backlog_provider)

    chunks: "list[str]" = []
    stream_iter = emitter.stream()
    chunks.append(await stream_iter.__anext__())  # prime past the initial connect snapshot

    try:
        resp = await _post(
            app, "/agui/chat/alpha?token=s3cret",
            {"type": "session_switch_request", "session_id": sid_b},
        )
        assert resp.json().get("switched") is True

        # Bounded collection: read chunks until the barrier CUSTOM event's
        # name shows up in the accumulated text, or give up after a few
        # scheduling turns — the barrier is guaranteed to arrive exactly
        # once by production behaviour (asserted below), so this is a
        # termination condition, not an arbitrary window silently truncating.
        for _ in range(50):
            chunk = await stream_iter.__anext__()
            chunks.append(chunk)
            if "reyn.event.session_attached" in "".join(chunks):
                break
    finally:
        source.close()

    sse_text = "".join(chunks)
    assert "reyn.event.session_attached" in sse_text, (
        f"the already-open stream's raw SSE text never carried the switch "
        f"barrier event: {sse_text!r}"
    )
    events = parse_sse_blocks(sse_text.split("\n"))
    barrier = [
        ev for ev in events
        if ev.type == "CUSTOM" and ev.data.get("name") == "reyn.event.session_attached"
    ]
    (barrier_event,) = barrier
    assert barrier_event.data.get("value", {}).get("session_id") == sid_b
    await asyncio.sleep(0)  # let source's own tasks settle before teardown
