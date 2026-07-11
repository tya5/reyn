"""Shared test helper — drive the web gateway as the local same-machine operator.

The P1 auth gate (``reyn.interfaces.web.auth_gate.AuthGateMiddleware``) now
authenticates every non-AG-UI surface (``/api``, ``/a2a``, ``/mcp``, the
resource-fetch routes) before the request reaches the router. Existing route /
protocol tests that drive the production app were written against the previously
UNauthenticated surfaces, so without a presented identity they now receive a
401.

Those tests exercise endpoint behaviour, not the auth gap, so the honest way to
keep them green is to present them as the **local same-machine operator** — the
default ``reyn web`` posture. :func:`local_operator_asgi` wraps the app so every
HTTP request arrives on the UDS peer-cred tier (client host absent), which the
P0 :meth:`AuthContext.authenticate` seam admits without a token (OS-fenced
same-machine access, unchanged by P1). It also ensures the app carries a P0
auth context even when the test deliberately bypasses the server lifespan
(``TestClient`` without ``with`` does not fire startup).

This is a transport simulation (a real ASGI app that sets the connection's
client host), not a mock of any collaborator: the real middleware, the real
``AuthContext``, and the real router all run.
"""
from __future__ import annotations

from typing import Any


def local_operator_asgi(app: Any) -> Any:
    """Wrap ``app`` so TestClient requests arrive as the local UDS operator.

    Ensures ``app.state.auth`` exists (so the gate has a context even without a
    lifespan run), then presents every HTTP request with no client host so the
    P0 auth seam classifies it as the same-machine UDS tier and admits it
    without a token. Lifespan / websocket / other scopes pass straight through.
    """
    from reyn.interfaces.web.auth import AuthContext

    if getattr(app.state, "auth", None) is None:
        app.state.auth = AuthContext()

    async def _asgi(scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["client"] = None  # UDS tier: same-machine, peer-cred admitted
        await app(scope, receive, send)

    return _asgi


def local_operator_client(app: Any, **kwargs: Any):
    """Return a ``TestClient`` that drives ``app`` as the local UDS operator."""
    from fastapi.testclient import TestClient

    return TestClient(local_operator_asgi(app), **kwargs)
