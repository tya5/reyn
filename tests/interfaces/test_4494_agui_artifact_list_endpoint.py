"""Tier 2: #4494 design C — the AG-UI endpoint's ``artifact_list_request``
ptype, and ``AgUiTransport.request_artifact_list``'s wire payload.

Mirrors ``test_4534_pr1_agui_attach_switch_endpoint.py``'s own fixture
shape (real ``AgentRegistry`` + real ASGI app over ``httpx.AsyncClient``)
and its closing-the-loop pattern (client's real output fed directly into
the real server endpoint — no hand-authored literal duplicated on both
sides).

**#4601**: the endpoint now caps entries (newest-first) at
``config.artifacts.remote_fallback_limit`` and returns ``total`` (the
pre-cap count) alongside them on the wire.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.data.workspace.artifact_ref import mint_ref
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _registry(tmp_path, monkeypatch, *, reyn_yaml: str = MINIMAL_REYN_YAML) -> AgentRegistry:
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(reyn_yaml, encoding="utf-8")
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
async def test_artifact_list_request_returns_the_agents_ref_table_entries(
    tmp_path, monkeypatch,
):
    """Tier 2: the happy path — reads the server's OWN copy of the
    durable table, scoped to the URL's agent_name, never a client-
    supplied path."""
    reg = _registry(tmp_path, monkeypatch)
    f = tmp_path / "report.pdf"
    f.write_text("x")
    ref = mint_ref(tmp_path, "alpha", f)
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret", {"type": "artifact_list_request"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("entries") == [{"ref": ref, "path": str(f)}]
    assert body.get("total") == 1


@pytest.mark.asyncio
async def test_artifact_list_request_returns_empty_with_no_ref_minted(
    tmp_path, monkeypatch,
):
    """Tier 2: (accept-side) an empty table -> [], not an error."""
    reg = _registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret", {"type": "artifact_list_request"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("entries") == []
    assert body.get("total") == 0


@pytest.mark.asyncio
async def test_artifact_list_request_caps_entries_at_the_configured_limit(
    tmp_path, monkeypatch,
):
    """Tier 2: (#4601) the ORIGINAL finding this issue reports — the
    endpoint used to return the table's FULL, ever-growing contents with
    no cap. Minting more refs than ``remote_fallback_limit`` proves the
    cap is real: entries truncated (newest-first), ``total`` still
    names the full count."""
    reg = _registry(
        tmp_path, monkeypatch,
        reyn_yaml=MINIMAL_REYN_YAML + "\nartifacts:\n  remote_fallback_limit: 2\n",
    )
    refs = []
    for i in range(5):
        f = tmp_path / f"f{i}.pdf"
        f.write_text("x")
        refs.append(mint_ref(tmp_path, "alpha", f))
    app = _build_app(reg, monkeypatch)

    resp = await _post(
        app, "/agui/chat/alpha?token=s3cret", {"type": "artifact_list_request"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [e["ref"] for e in body["entries"]] == [refs[4], refs[3]]
    assert body["total"] == 5


@pytest.mark.asyncio
async def test_agui_transport_request_artifact_list_sends_the_typed_payload():
    """Tier 2: AgUiTransport.request_artifact_list sends the SAME typed
    shape the server's artifact_list_request arm expects."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    sent: list = []

    async def _send(payload: dict) -> dict:
        sent.append(payload)
        return {"entries": [{"ref": "r1", "path": "/p/x.pdf"}], "total": 1}

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    entries, total = await transport.request_artifact_list(agent="alpha")

    assert entries == [{"ref": "r1", "path": "/p/x.pdf"}]
    assert total == 1
    assert sent == [{"type": "artifact_list_request"}]


@pytest.mark.asyncio
async def test_agui_transport_request_artifact_list_empty_when_send_returns_none():
    """Tier 2: (accept-side) a None/falsy send result -> ([], 0), not a
    crash."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    async def _send(payload: dict):
        return None

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    assert await transport.request_artifact_list(agent="alpha") == ([], 0)


# ── closing the wire-shape double-transcription gap (mirrors #4537/#4534) ──


@pytest.mark.asyncio
async def test_agui_transport_artifact_list_payload_is_accepted_by_the_real_endpoint(
    tmp_path, monkeypatch,
):
    """Tier 2: the client's own request_artifact_list output, fed
    directly into the real server endpoint — no hand-authored literal
    duplicated on either side."""
    from reyn.interfaces.transport.agui.client import AgUiTransport

    reg = _registry(tmp_path, monkeypatch)
    f = tmp_path / "report.pdf"
    f.write_text("x")
    ref = mint_ref(tmp_path, "alpha", f)
    app = _build_app(reg, monkeypatch)

    async def _send(payload: dict) -> dict:
        resp = await _post(app, "/agui/chat/alpha?token=s3cret", payload)
        return resp.json()

    async def _empty_lines():
        return
        yield  # pragma: no cover

    transport = AgUiTransport(_empty_lines(), _send)
    entries, total = await transport.request_artifact_list(agent="alpha")

    assert entries == [{"ref": ref, "path": str(f)}]
    assert total == 1
