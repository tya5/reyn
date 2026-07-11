"""ASGI auth gate — mount-front authentication for the gateway's non-AG-UI surfaces.

The web gateway's AG-UI transport self-gates every handler on the P0 auth
context, but the REST control plane (``/api``), the A2A spine (``/a2a``), the
MCP surface (``/mcp``), and the resource-fetch routes
(``/agents/<a>/tool-results/<artifact>``) had **no** authentication check: the
app's only middleware was CORS. On a loopback bind that is harmless (the OS
already fences same-machine access), but ``reyn web`` supports a non-loopback
bind for cross-machine use over TLS + token — and there those surfaces were
reachable **unauthenticated**. The sharpest exposure is
``PATCH /api/budget/caps`` (raise the cap → disable budget bounding → unbounded
spend); ``DELETE /api/permissions/<key>`` (revoke approvals) and the
``/api/agents`` + ``/api/topologies`` control-plane mutations are the same
class.

This module closes that gap with **one** ASGI middleware that reuses the
existing P0 auth substrate (it introduces no new authentication):

  - :func:`surface_class_for` maps a request's path prefix to the surface's
    identity class (operator / peer / client / resource), or ``None`` for the
    surfaces that stay open (the OpenUI shell assets, ``/health``, the
    self-gated ``/agui`` transport, and the native-HMAC webhook plugins).
  - :class:`AuthGateMiddleware` resolves every gated request's identity through
    the SAME :func:`~reyn.interfaces.transport.agui.endpoint.authenticate_request`
    seam the AG-UI HTTP handlers use — client host + presented token
    (``?token=`` / ``Authorization: Bearer``) → :meth:`AuthContext.authenticate`.
    An unauthenticated request is answered with a 401 **before** it reaches the
    router; an authenticated one has its :class:`ConnectionIdentity` (and the
    surface class) stamped onto ``request.state`` for downstream fencing + audit.

Placement invariant: this middleware sits **inside** the CORS middleware (CORS
stays outermost) so a CORS preflight ``OPTIONS`` is answered without a token.
``OPTIONS`` is never gated here for the same reason. The middleware is a pure
addition: it does not touch the AG-UI per-handler gate (``/agui`` is skipped and
stays byte-identical), it does not change the A2A ``external_source`` fence
(the identity says *who* the connection is, not *how* its writes are fenced),
and it inherits the P0 tier posture unchanged — same-machine UDS peer-cred
connections authenticate without a token, loopback/network connections present
the token.
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from reyn.interfaces.transport.agui.endpoint import authenticate_request

logger = logging.getLogger(__name__)


# Path-prefix → surface identity class. The class is derived from the surface,
# not a second credential: with a single operator token v1 authenticates the
# same token across every gated surface, and the class is carried for downstream
# fencing + audit only (per-class credentials are a future follow-up).
_GATED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/", "operator"),  # REST control plane
    ("/a2a/", "peer"),      # A2A spine (fenced downstream, unchanged)
    ("/mcp/", "client"),    # MCP surface
)

# The resource-fetch routes live at top-level ``/agents/<a>/tool-results/<...>``
# (NOT under ``/api``); an authenticated peer legitimately fetches a path_ref, so
# ANY authenticated class is admitted (not operator-only).
_RESOURCE_PREFIX = "/agents/"
_RESOURCE_MARKER = "/tool-results/"


def surface_class_for(path: str, method: str) -> str | None:
    """Return the identity class for a gated request, or ``None`` if it is open.

    ``OPTIONS`` is never gated: a CORS preflight must be answered without a
    token (CORS itself, mounted outermost, handles real preflights; this keeps
    a route's own ``OPTIONS`` handler reachable too). Every non-gated prefix —
    the OpenUI shell (``/``, ``/static``, ``/web/designs``), ``/health``, the
    self-gated ``/agui`` transport, and the native-HMAC ``/webhook`` plugins —
    falls through to ``None`` (open).
    """
    if method == "OPTIONS":
        return None
    for prefix, identity_class in _GATED_PREFIXES:
        if path.startswith(prefix):
            return identity_class
    if path.startswith(_RESOURCE_PREFIX) and _RESOURCE_MARKER in path:
        return "resource"
    return None


class AuthGateMiddleware:
    """Authenticate every gated non-AG-UI request before it reaches the router.

    A pure ASGI middleware (so it can 401 *before* the request body is read and
    before the router dispatches). It reads nothing the client asserts about its
    own identity: the client host, presented token, and tier all come from the
    server-side connection via the reused P0 seam.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        identity_class = surface_class_for(request.url.path, request.method)
        if identity_class is None:
            # Open surface (shell / health / agui / webhook): pass through.
            await self.app(scope, receive, send)
            return

        auth = getattr(getattr(request.app, "state", None), "auth", None)
        if auth is None:
            # Fail-closed: a gated surface with no auth context is refused (the
            # AG-UI gate makes the same choice). The context is built once per
            # process in the server lifespan.
            await JSONResponse(
                {"error": "authentication unavailable"}, status_code=401
            )(scope, receive, send)
            return

        identity = authenticate_request(request, auth)
        if not identity.authenticated:
            await JSONResponse(
                {"error": "authentication required"}, status_code=401
            )(scope, receive, send)
            return

        # Authenticated: stamp identity + surface class for downstream fencing
        # and audit, then hand off to the router (same scope/receive — the body
        # is untouched here).
        request.state.identity = identity
        request.state.identity_class = identity_class
        await self.app(scope, receive, send)


__all__ = ["AuthGateMiddleware", "surface_class_for"]
