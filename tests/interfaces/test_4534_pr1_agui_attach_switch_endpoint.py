"""Tier 2: #4534 PR-1 — the AG-UI endpoint's ``attach_request``/
``session_switch_request`` ptype arms.

Mirrors ``test_3595_s5_session_does_not_interpret_text.py``'s own
``test_a_remote_client_can_still_run_a_command`` fixture shape (real
``AgentRegistry`` + real ASGI app over ``httpx.AsyncClient`` — no mocks),
applied to the two new #4534 PR-1 wire ptypes.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
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
    reg.create("beta")
    reg.get_or_load("alpha")
    return reg


async def _post(app, path: str, payload: dict):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.post(path, json=payload)


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


@pytest.mark.asyncio
async def test_attach_request_ptype_reaches_registry_attach(tmp_path, monkeypatch):
    """Tier 2: the happy path — POSTing attach_request with a real target
    agent name actually attaches it, mirroring the __attach_request__
    sentinel's own registry.attach() call."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret",
        {"type": "attach_request", "agent_name": "beta"},
    )
    assert resp.status_code == 200
    assert resp.json().get("attached") is True
    assert reg.attached_name == "beta"


@pytest.mark.asyncio
async def test_attach_request_ptype_rejects_unknown_agent(tmp_path, monkeypatch):
    """Tier 2: (accept-side) an unknown agent name answers attached=False,
    not a crash — a client on a different build, not corruption."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret",
        {"type": "attach_request", "agent_name": "nonexistent"},
    )
    assert resp.status_code == 200
    assert resp.json().get("attached") is False


@pytest.mark.asyncio
async def test_session_switch_request_ptype_reaches_registry_attach_session(
    tmp_path, monkeypatch,
):
    """Tier 2: the happy path — POSTing session_switch_request with a real
    spawned sid actually focuses it, mirroring the
    __session_switch_request__ sentinel's own registry.attach_session()
    call."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    sid = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
    await reg.attach("alpha")
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret",
        {"type": "session_switch_request", "session_id": sid},
    )
    assert resp.status_code == 200
    assert resp.json().get("switched") is True
    assert reg.attached_sid == sid


@pytest.mark.asyncio
async def test_session_switch_request_ptype_rejects_unknown_sid(tmp_path, monkeypatch):
    """Tier 2: (accept-side) an unknown sid answers switched=False,
    mirroring the sentinel branch's own KeyError-to-noop tolerance."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    await reg.attach("alpha")
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret",
        {"type": "session_switch_request", "session_id": "no-such-sid"},
    )
    assert resp.status_code == 200
    assert resp.json().get("switched") is False


# ── AgUiTransport (client side) — the wire payload it builds ─────────────


@pytest.mark.asyncio
async def test_agui_transport_request_attach_sends_the_typed_payload():
    """Tier 2: AgUiTransport.request_attach sends the SAME typed shape
    the server's attach_request arm expects (name-only payload, never a
    raw sentinel string) — the client half of the wire contract,
    complementing the server-side endpoint tests above."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    sent: list = []

    async def _send(payload: dict) -> dict:
        sent.append(payload)
        return {"attached": True}

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    ok = await transport.request_attach("beta")

    assert ok is True
    assert sent == [{"type": "attach_request", "agent_name": "beta"}]


@pytest.mark.asyncio
async def test_agui_transport_request_session_switch_sends_the_typed_payload():
    """Tier 2: same contract as the attach test above, for the
    session-switch wire shape."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    sent: list = []

    async def _send(payload: dict) -> dict:
        sent.append(payload)
        return {"switched": True}

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    ok = await transport.request_session_switch("sid-123")

    assert ok is True
    assert sent == [{"type": "session_switch_request", "session_id": "sid-123"}]


@pytest.mark.asyncio
async def test_agui_transport_request_attach_false_when_send_returns_none():
    """Tier 2: (accept-side) a None/falsy send result -> False, not a
    crash — mirrors run_slash_command's own `(result or {}).get(...)`
    tolerance for a transport with no active connection."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    async def _send(payload: dict):
        return None

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    assert await transport.request_attach("beta") is False


# ── closing the wire-shape double-transcription gap (lead-coder #4537 review) ──
#
# The client-side tests above and the endpoint tests further up each
# independently hand-wrote the SAME literal payload shape
# ({"type": "attach_request", "agent_name": ...} etc.) — a contract
# double-transcription (CLAUDE.md Q2's variant: not "the implementation,
# transcribed" but "the CONTRACT, transcribed twice"). A client-side field
# rename (agent_name -> agentName) would leave BOTH sides green
# independently, since neither ever actually sends the OTHER side's
# literal. These two tests close that: AgUiTransport's real `_send` is
# wired DIRECTLY to the real ASGI app, so whatever the client ACTUALLY
# constructs is what gets POSTed — one source of truth, not two.


@pytest.mark.asyncio
async def test_agui_transport_attach_payload_is_accepted_by_the_real_endpoint(
    tmp_path, monkeypatch,
):
    """Tier 2: the client's own request_attach output, fed directly into
    the real server endpoint — no hand-authored literal on either side."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    reg = _two_agent_registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)

    async def _send(payload: dict) -> dict:
        resp = await _post(app, "/agui/chat/alpha?token=s3cret", payload)
        return resp.json()

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    ok = await transport.request_attach("beta")

    assert ok is True
    assert reg.attached_name == "beta"


@pytest.mark.asyncio
async def test_agui_transport_session_switch_payload_is_accepted_by_the_real_endpoint(
    tmp_path, monkeypatch,
):
    """Tier 2: same closing-the-loop shape as the attach test above, for
    request_session_switch."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    reg = _two_agent_registry(tmp_path, monkeypatch)
    sid = reg.spawn_session("alpha", presentation_consumer=None, intervention_bridge=None)
    await reg.attach("alpha")
    app = _build_app(reg, monkeypatch)

    async def _send(payload: dict) -> dict:
        resp = await _post(app, "/agui/chat/alpha?token=s3cret", payload)
        return resp.json()

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    ok = await transport.request_session_switch(sid)

    assert ok is True
    assert reg.attached_sid == sid
