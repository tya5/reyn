"""Tier 2: #5116 — an already-open remote SSE stream follows a CROSS-agent
``attach_request`` (the owner's own live-reported bug: status bar / history
stayed on the connection's ORIGINAL agent after ``/attach``, despite the
POST reporting success and the REGISTRY's own global pointer genuinely
switching).

Root cause (e2e-coder's own decisive raw observation, #5094
issuecomment-5380435298 — a real SSE tap, no client-side reinterpretation):
``registry.add_attach_listener`` is keyed by AGENT NAME, correct for a
same-agent session-switch (the connection is already listening under its
own agent's key) but structurally unable to notify a connection about a
DIFFERENT agent (nobody is registered under the TARGET's key). Confirmed
live: zero ``session_attached`` frames reached the connection at all after
a cross-agent ``/attach`` — not "the wrong agent name in the payload", the
announce never fired for this connection in the first place.

Fix (architect ruling, "lifting state up"/"unidirectional"/"no derived
state"/"push not pull", issuecomment-5380440608 family):
``_SessionFrameSource`` becomes the single per-connection owner of "which
agent, which session" — a NEW connection-id-keyed channel
(``_ConnectionRetargetHub``) notifies it directly (the ``attach_request``
POST handler is a DIFFERENT HTTP request than this SSE stream, correlated
only by ``connection_id``, and is the one caller with both the id and the
resolved target). The announce this source emits now NAMES WHATEVER IT WAS
JUST TOLD (``self._agent_name`` is updated BEFORE the announce is built,
never read-then-written-back), and ``_status_provider`` reads
``source.current_session()`` fresh every call rather than a ``session``
variable closed over at connect time.

Chains the REAL pieces, mirroring ``test_4534_pr2b_switch_follow_e2e.py``'s
own pattern (that file's own docstring names it as the gate that would
have caught a silently-orphaned mechanism — this is its cross-agent
sibling): a REAL httpx ASGI POST to ``attach_request`` -> the REAL
``_ConnectionRetargetHub`` -> a REAL, already-``start()``ed
``_SessionFrameSource``'s dual-wait -> a REAL ``AgUiEmitter`` mid-
``stream()`` re-firing the reconnect protocol, over the SAME in-memory
``AgentRegistry``. No mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import (
    _SessionFrameSource,
    session_backlog_frames,
)
from reyn.interfaces.transport.agui.protocol import parse_sse_blocks
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_CONNECTION_ID = "conn-5116-test"


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
    reg.create("coder-smith")
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
    out: list = []
    it = agen.__aiter__()
    while True:
        item = await it.__anext__()
        out.append(item)
        if isinstance(item, EventFrame) and getattr(item.event, "type", "") == "session_attached":
            return out


@pytest.mark.asyncio
async def test_open_sse_stream_follows_a_cross_agent_attach_request_post(
    tmp_path, monkeypatch,
):
    """Tier 2: #5116 witness — status/announce/frame all agree after a
    REAL cross-agent ``attach_request`` POST, driven through the wire
    ptype (not ``registry.attach`` called directly)."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    default_session = await reg.attach("default")
    app = _build_app(reg, monkeypatch)

    # The "already-open stream": bound to "default", listening for a
    # cross-agent retarget under ITS OWN connection id — exactly the
    # server-side state a --connect client's SSE GET leaves behind.
    source = _SessionFrameSource(default_session, registry=reg, agent_name="default")
    source.listen_for_retarget(_CONNECTION_ID)
    source.start()
    frames_iter = source.frames()

    # ★ status BEFORE the attach: reads "default" — the pre-condition a
    # naive fix could accidentally satisfy by coincidence (e.g. session
    # objects that happen to look alike). Asserted so the AFTER read
    # below is a genuine change, not a tautology.
    status_before = _snapshot_for_session(reg, source.current_session())
    assert status_before["attached_name"] == "default"

    try:
        # The real wire entry point — the SAME ptype ClientTransport.
        # request_attach sends, WITH this connection's own id (the
        # correlation attach_request's handler uses to find the right
        # _SessionFrameSource — a different id would prove nothing).
        resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "attach_request", "agent_name": "coder-smith"},
        )
        assert resp.status_code == 200
        assert resp.json().get("attached") is True
        # The registry's OWN global pointer — a SEPARATE fact (#3793
        # stage 2), asserted here only as a positive control that the
        # attach itself genuinely happened server-side.
        assert reg.attached_name == "coder-smith"

        collected = await _collect_until_barrier(frames_iter)
    finally:
        source.close()

    # ── ① announce ──────────────────────────────────────────────────────
    barrier = [
        f for f in collected
        if isinstance(f, EventFrame) and getattr(f.event, "type", "") == "session_attached"
    ]
    assert barrier, (
        f"the already-open stream never observed the cross-agent attach — "
        f"no session_attached EventFrame arrived; collected {len(collected)} "
        f"item(s): {collected!r}"
    )
    assert barrier[0].event.data.get("agent") == "coder-smith", (
        f"the announce must name the TARGET agent, not the connection's "
        f"original one — got {barrier[0].event.data!r}"
    )

    # ── ② status (the owner's own reported symptom: "status bar stays on "
    #     "default") ───────────────────────────────────────────────────
    status_after = _snapshot_for_session(reg, source.current_session())
    assert status_after["attached_name"] == "coder-smith", (
        f"status must reflect the NEW agent after attach; got "
        f"{status_after['attached_name']!r} (still the pre-attach value "
        f"means _status_provider is reading a frozen/stale session)"
    )

    # ── ③ frame source's own current-agent read path ────────────────────
    assert source.current_agent_name() == "coder-smith"
    assert source.current_session() is reg.get_session("coder-smith", "main") or (
        source.current_session().agent_name == "coder-smith"
    )

    await asyncio.sleep(0)  # let source's own tasks settle before teardown


@pytest.mark.asyncio
async def test_status_changes_are_pushed_by_the_attach_itself_not_ridden_on_a_later_frame(
    tmp_path, monkeypatch,
):
    """Tier 2: #5116's own push-vs-pull witness (architect/lead-coder,
    issuecomment-5380440608/5380440... family — owner's verbatim: "stop
    designing old-generation query-on-demand UIs"). The projected STATE_
    DELTA reaching the wire text must be driven by the attach's OWN
    announce frame — not require some LATER, UNRELATED content frame to
    arrive before status catches up (the pre-#5116 pull shape: status was
    only ever recomputed as a side effect of whatever frame happened to
    stream next).

    This is a genuine push claim, not merely "eventually consistent": the
    ONLY frame this test lets flow after the attach is the announce
    itself (bounded collection stops at the first STATE_DELTA/session_
    attached pair) — no chat message, no tool call, nothing else is ever
    submitted."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    default_session = await reg.attach("default")
    app = _build_app(reg, monkeypatch)

    source = _SessionFrameSource(default_session, registry=reg, agent_name="default")
    source.listen_for_retarget(_CONNECTION_ID)
    source.start()

    def _backlog_provider(name: str, sid: str):
        return session_backlog_frames(reg, name, sid)

    emitter = AgUiEmitter(
        source.frames(),
        lambda: _snapshot_for_session(reg, source.current_session()),
        backlog_provider=_backlog_provider,
    )

    chunks: "list[str]" = []
    stream_iter = emitter.stream()
    chunks.append(await stream_iter.__anext__())  # prime past the initial connect snapshot

    try:
        resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "attach_request", "agent_name": "coder-smith"},
        )
        assert resp.json().get("attached") is True

        # Bounded on the RE-FIRED STATE_SNAPSHOT itself arriving, not just
        # the announce's own text (#3310 N3: the emitter's re-fire yields
        # the announce frame FIRST, then a SEPARATE MESSAGES_SNAPSHOT +
        # STATE_SNAPSHOT pair in the SAME loop iteration — stopping at the
        # announce alone would read a chunk too early and assert on
        # whatever STATE_SNAPSHOT happened to precede it, not the pushed
        # one). A real termination condition (guaranteed exactly once by
        # production behaviour, asserted below), never an unrelated
        # content frame this test would otherwise have to fabricate to
        # "unstick" a pull design.
        for _ in range(50):
            chunk = await stream_iter.__anext__()
            chunks.append(chunk)
            joined = "".join(chunks)
            if "reyn.event.session_attached" in joined and "STATE_SNAPSHOT" in joined.split(
                "reyn.event.session_attached", 1,
            )[1]:
                break
    finally:
        source.close()

    sse_text = "".join(chunks)
    events = parse_sse_blocks(sse_text.split("\n"))
    state_events = [ev for ev in events if ev.type in ("STATE_SNAPSHOT", "STATE_DELTA")]
    assert state_events, (
        f"no STATE_SNAPSHOT/STATE_DELTA reached the wire alongside the "
        f"attach's own announce — status did not push; sse_text={sse_text!r}"
    )
    # The LAST state event before/around the barrier must carry the NEW
    # agent's attached_name — proves the status push read the RETARGETED
    # session, not a stale snapshot re-sent unchanged. STATE_SNAPSHOT
    # nests the projected dict under "snapshot"; STATE_DELTA under
    # "delta" (encode_state_snapshot/encode_state_delta, protocol.py).
    last_snapshot = next(
        (
            ev.data.get("snapshot") or ev.data.get("delta")
            for ev in reversed(state_events)
            if isinstance(ev.data, dict)
        ),
        None,
    )
    assert last_snapshot is not None
    assert last_snapshot.get("attached_name") == "coder-smith", (
        f"the pushed status must already reflect the new agent; got "
        f"{last_snapshot!r}"
    )

    await asyncio.sleep(0)
