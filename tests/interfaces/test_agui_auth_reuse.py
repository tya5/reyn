"""Tier 2: the AG-UI endpoint reuses the P0 auth gate — unauth refused (P2, D5a).

The new SSE surface introduces no new auth: it resolves identity through the same
:class:`~reyn.interfaces.web.auth.core.AuthContext.authenticate` seam the WS gate
uses. This pins the security invariant: a connection with no / a wrong token is
refused (401) BEFORE any session is attached; a connection presenting the
configured token passes the gate (it is not refused for auth).

Real instances only — a real FastAPI app mounting the real router with a real
AuthContext; the Starlette TestClient drives real HTTP. No mocks.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reyn.interfaces.transport.agui.endpoint import router
from reyn.interfaces.web.auth import AuthContext


def _app_with_token(token: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    # A network-tier (non-loopback) client must present the token; require_token
    # forces the check even were the client loopback.
    app.state.auth = AuthContext(token=token, require_token=True)
    return app


def test_submit_without_token_is_refused() -> None:
    """Tier 2: POST with no token is refused (401) before any session work."""
    client = TestClient(_app_with_token("s3cret"))
    resp = client.post("/agui/chat/demo", json={"type": "user_message", "text": "hi"})
    assert resp.status_code == 401


def test_submit_with_wrong_token_is_refused() -> None:
    """Tier 2: POST with a wrong token is refused (401)."""
    client = TestClient(_app_with_token("s3cret"))
    resp = client.post(
        "/agui/chat/demo",
        json={"type": "user_message", "text": "hi"},
        headers={"authorization": "Bearer nope"},
    )
    assert resp.status_code == 401


def test_events_without_token_is_refused() -> None:
    """Tier 2: the SSE GET with no token is refused (401) before attach."""
    client = TestClient(_app_with_token("s3cret"))
    resp = client.get("/agui/chat/demo/events")
    assert resp.status_code == 401


def test_correct_token_passes_the_auth_gate() -> None:
    """Tier 2: with the configured token the connection is NOT refused for auth
    (it passes the gate; a missing agent is a 404, not a 401)."""
    client = TestClient(_app_with_token("s3cret"))
    resp = client.post(
        "/agui/chat/demo?token=s3cret",
        json={"type": "user_message", "text": "hi"},
    )
    assert resp.status_code != 401
    # It got past auth into agent resolution (demo agent does not exist here).
    assert resp.status_code in (403, 404)
