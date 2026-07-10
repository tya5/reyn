"""Tier 2c: the WebSocket answer/permission path is gated behind authentication.

ADR-0039 P0 invariants 1 + 5. The retrofit: the existing ``/ws/chat`` endpoint
must now REQUIRE an authenticated connection — an unauthenticated client is
rejected at the handshake and can never reach the answer/grant path. And the
answer/attach/detach audit-events stamp the connection identity so a permission
grant is attributable.

Real FastAPI app + real AgentRegistry + real Session throughout (the LLM is
never invoked — attach only boots the idle run-loop); no mocks. The unauth
rejection is end-to-end through the live route.

Falsification: delete the ``if not identity.authenticated`` gate in ``ws_chat``
and ``test_unauthenticated_connection_is_rejected`` goes RED (the existing agent
connects without a token).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from reyn.core.events.events import Event, EventLog  # noqa: E402
from reyn.core.events.state_log import StateLog  # noqa: E402
from reyn.interfaces.web import deps as web_deps  # noqa: E402
from reyn.interfaces.web.auth import OPERATOR_USER_ID, AuthContext, TransportTier  # noqa: E402
from reyn.interfaces.web.ws import chat as ws_chat  # noqa: E402
from reyn.runtime.profile import AgentProfile  # noqa: E402
from reyn.runtime.registry import AgentRegistry  # noqa: E402
from reyn.runtime.session import Session  # noqa: E402

_TOKEN = "the-gateway-secret"


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    AgentProfile.new("a", role="").save(tmp_path / ".reyn" / "agents" / "a")
    return reg


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_chat.router)
    app.state.auth = AuthContext(token=_TOKEN, server_uid=1000)
    return app


def test_unauthenticated_connection_is_rejected(tmp_path, monkeypatch):
    """Tier 2c: an existing agent connected WITHOUT a token is rejected (invariant 1)."""
    monkeypatch.setattr(web_deps, "_registry", _make_registry(tmp_path))
    client = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat/a") as ws:
            ws.close()


def test_authenticated_connection_is_accepted(tmp_path, monkeypatch):
    """Tier 2c: the same agent connected WITH the token is accepted (retrofit falsify pair)."""
    monkeypatch.setattr(web_deps, "_registry", _make_registry(tmp_path))
    client = TestClient(_make_app())
    # No WebSocketDisconnect at the handshake = the auth gate admitted the token.
    with client.websocket_connect(f"/ws/chat/a?token={_TOKEN}") as ws:
        ws.close()


def test_wrong_token_connection_is_rejected(tmp_path, monkeypatch):
    """Tier 2c: an existing agent connected with the WRONG token is rejected."""
    monkeypatch.setattr(web_deps, "_registry", _make_registry(tmp_path))
    client = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat/a?token=not-the-secret") as ws:
            ws.close()


def test_emit_audit_stamps_identity(tmp_path):
    """Tier 2c: a gateway audit-event carries the connection identity (invariant 5).

    ``_emit_audit`` forwards to the session's real EventLog; the emitted event
    must carry the authenticated user-id + connection-id + tier so a permission
    grant (an answer) is attributable to the identity that made it.
    """
    captured: list[Event] = []
    events = EventLog(subscribers=[captured.append])

    class _SessionCarryingEvents:
        _chat_events = events

    auth = AuthContext(token=_TOKEN, server_uid=1000)
    identity = auth.authenticate(
        client_host="203.0.113.5", presented_token=_TOKEN, connection_id="conn-42",
    )

    ws_chat._emit_audit(
        _SessionCarryingEvents(), "web_intervention_answered", identity, agent_name="a",
    )

    event_types = {e.type for e in captured}
    assert "web_intervention_answered" in event_types
    stamped = next(e for e in captured if e.type == "web_intervention_answered")
    assert stamped.data["auth_user_id"] == OPERATOR_USER_ID
    assert stamped.data["auth_connection_id"] == "conn-42"
    assert stamped.data["auth_tier"] == TransportTier.NETWORK.value
    assert stamped.data["agent_name"] == "a"
