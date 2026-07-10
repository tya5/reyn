"""The server-side authentication core for the web/thin-client gateway.

Every connection to the gateway CARRIES an identity, and every answer /
permission-grant path is authorized by that identity. This module is the
security layer that makes both true:

  - :class:`ConnectionIdentity` — the identity a connection carries. v1 has a
    single operator user-id (:data:`OPERATOR_USER_ID`); the ``user_id`` field
    exists from the start so multi-user authorization is a later authz-table
    extension, not a re-architecture.
  - :class:`TransportTier` — the secure-by-default tier a connection arrived
    on: in-process (T1), UDS (T2 default), loopback TCP (T2 fallback), or
    cross-machine network (T3).
  - :class:`AuthContext` — the process-wide policy object (built once at
    startup, read on every connection). It classifies a connection, resolves
    its identity (per-OS peer-cred on UDS; constant-time token compare on
    loopback/network), and answers the two authorization questions the gateway
    asks: *may this connection open at all?* and *may this connection submit an
    answer / grant?*
  - :func:`check_startup_binding` — the **fail-closed** guard: a non-loopback
    bind WITHOUT a configured token refuses to start, closing the
    accidental-exposure hole (``--host 0.0.0.0`` with no auth).

Keystone: an AUTHENTICATED connection's identity determines fencing. The v1
single operator identity is unfenced (``external_source=False``) — the same
treatment the local operator has always had, now gated behind authentication.
Untrusted A2A peer answers are a different trust class and stay fenced; this
module never touches that path.

The write authorization is deliberately a small ``identity -> bool`` predicate
evaluated **server-side at delivery time** (not trusting any client-sent
state), so a later phase that adds seize/takeover can reject a deposed holder's
in-flight answer at the same seam without re-plumbing.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from enum import Enum

from reyn.interfaces.web.auth.peercred import peer_uid_from_socket

# ── the v1 single operator identity ─────────────────────────────────────────
# One user-id today (operator == unfenced). Carried as a field so per-user-id
# authorization is a later table lookup, not a structural change.
OPERATOR_USER_ID = "operator"

# Hosts treated as same-machine loopback. Empty / None client host means a UDS
# connection (uvicorn reports no client address for a UNIX socket).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

# The env var the CLI uses to hand the effective token to the app process
# (the app is imported by uvicorn via an import string, so an in-memory object
# cannot be threaded directly — this mirrors REYN_WEB_EAGER_EMBEDDING_BUILD).
TOKEN_ENV_VAR = "REYN_WEB_AUTH_TOKEN"


class TransportTier(str, Enum):
    """The secure-by-default transport tier a connection arrived on."""

    IN_PROCESS = "in_process"  # T1 — same process, operator's own
    UDS = "uds"                # T2 default — same-machine, OS peer-cred gated
    LOOPBACK = "loopback"      # T2 fallback — loopback TCP (opt-in)
    NETWORK = "network"        # T3 — cross-machine, token + TLS


class AuthStartupError(RuntimeError):
    """A bind configuration would be insecure — the server must refuse to start."""


@dataclass(frozen=True)
class ConnectionIdentity:
    """The identity a single connection carries.

    ``authenticated`` is the gate every write path consults. ``user_id`` is the
    authorization anchor (``None`` when unauthenticated). ``peer_uid`` is the
    OS-verified UID on the UDS tier, stamped into the audit trail when known.
    """

    tier: TransportTier
    authenticated: bool
    user_id: str | None = None
    peer_uid: int | None = None
    connection_id: str = ""

    @property
    def is_operator(self) -> bool:
        return self.authenticated and self.user_id == OPERATOR_USER_ID

    def audit_fields(self) -> dict:
        """The identity fields stamped onto answer / attach / detach events."""
        return {
            "auth_user_id": self.user_id,
            "auth_tier": self.tier.value,
            "auth_connection_id": self.connection_id,
            "auth_peer_uid": self.peer_uid,
        }


def classify_transport(client_host: str | None) -> TransportTier:
    """Classify a connection by its client host.

    No client host (``None`` / ``""``) means a UDS connection. A loopback host
    is the T2 TCP fallback; anything else is a cross-machine T3 network peer.
    """
    if not client_host:
        return TransportTier.UDS
    if client_host in _LOOPBACK_HOSTS:
        return TransportTier.LOOPBACK
    return TransportTier.NETWORK


def is_loopback_host(host: str | None) -> bool:
    """True iff *host* is a same-machine loopback address (or empty = UDS)."""
    return not host or host in _LOOPBACK_HOSTS


def generate_token() -> str:
    """Return a fresh URL-safe bearer token (Jupyter-style launch secret)."""
    return secrets.token_urlsafe(32)


def verify_token(expected: str | None, presented: str | None) -> bool:
    """Constant-time compare of a presented token against the expected one.

    Returns ``False`` whenever either side is empty so a missing token never
    authenticates.
    """
    if not expected or not presented:
        return False
    return secrets.compare_digest(str(expected), str(presented))


def check_startup_binding(host: str, *, token: str | None, uds: bool) -> None:
    """Fail-closed bind guard — raise :class:`AuthStartupError` on exposure.

    A **non-loopback TCP bind WITHOUT a configured token** is the
    accidental-exposure hole (``reyn web --host 0.0.0.0`` with no auth would
    let any network client act as the operator). It refuses to start. UDS binds
    (same-machine, owner-only run-dir gated) and loopback binds are allowed.
    """
    if uds:
        return
    if is_loopback_host(host):
        return
    if not token:
        raise AuthStartupError(
            f"Refusing to start: --host {host!r} is a non-loopback bind with no "
            "authentication token configured. A network-reachable gateway must "
            "have web.auth.token set (T3 token handshake). Bind to 127.0.0.1 for "
            "local use, or configure a token to expose it deliberately."
        )


@dataclass
class AuthContext:
    """Process-wide auth policy: classify a connection and authorize it.

    Built once at startup and read on every connection. ``token`` is the
    effective bearer secret (operator-configured or startup-generated).
    ``server_uid`` anchors the UDS peer-cred comparison. ``require_token``
    forces even loopback connections to present the token (the secure default
    for the browser surface, which always has a startup-issued token).
    """

    token: str | None = None
    server_uid: int | None = field(default_factory=lambda: _safe_getuid())
    require_token: bool = True

    @classmethod
    def from_env_and_config(cls, config) -> "AuthContext":
        """Build the context from ``web.auth`` config + the token env var.

        The CLI writes the effective token to :data:`TOKEN_ENV_VAR`; an operator
        may instead set it in ``web.auth.token``. When neither is present a
        token is generated so the surface is never left unauthenticated — the
        generated value is logged by the caller so a direct-``uvicorn`` launch
        can still connect.
        """
        auth_cfg = getattr(getattr(config, "web", None), "auth", None)
        token = os.environ.get(TOKEN_ENV_VAR) or getattr(auth_cfg, "token", None)
        require_token = getattr(auth_cfg, "require_token_on_loopback", True)
        generated = False
        if not token:
            token = generate_token()
            generated = True
        ctx = cls(token=token, require_token=bool(require_token))
        ctx.token_was_generated = generated  # type: ignore[attr-defined]
        return ctx

    # ── authentication ──────────────────────────────────────────────────────

    def authenticate(
        self,
        *,
        client_host: str | None,
        presented_token: str | None,
        peer_uid: int | None = None,
        connection_id: str = "",
    ) -> ConnectionIdentity:
        """Resolve the identity of a connection (server-side; client-untrusted).

        UDS: OS-gated — the socket lives in an owner-only (``0700``) run
        directory that admits only the operator UID (macOS ignores a socket's
        own file-mode on ``connect``, so the parent dir is the enforceable
        gate). When a peer UID is readable it must match the server UID, else
        the connection
        is unauthenticated (defense in depth). Loopback / network: the token is
        required and compared in constant time. All authenticated connections
        resolve to the SAME operator user-id, so authorization is uniform across
        a concurrent T2+T3 bind.
        """
        tier = classify_transport(client_host)
        if tier is TransportTier.UDS:
            if (
                peer_uid is not None
                and self.server_uid is not None
                and peer_uid != self.server_uid
            ):
                return ConnectionIdentity(
                    tier=tier,
                    authenticated=False,
                    peer_uid=peer_uid,
                    connection_id=connection_id,
                )
            return ConnectionIdentity(
                tier=tier,
                authenticated=True,
                user_id=OPERATOR_USER_ID,
                peer_uid=peer_uid,
                connection_id=connection_id,
            )
        # Loopback / network tiers: authenticate by token.
        if verify_token(self.token, presented_token):
            return ConnectionIdentity(
                tier=tier,
                authenticated=True,
                user_id=OPERATOR_USER_ID,
                connection_id=connection_id,
            )
        return ConnectionIdentity(
            tier=tier,
            authenticated=False,
            connection_id=connection_id,
        )

    def authenticate_ws(self, websocket, *, connection_id: str = "") -> ConnectionIdentity:
        """Authenticate a Starlette/FastAPI WebSocket before ``accept``.

        Reads the client host, the presented token (``?token=`` query param or
        an ``Authorization: Bearer`` header), and a best-effort peer UID, then
        delegates to :meth:`authenticate`. All inputs come from the server-side
        connection object — nothing the client asserts about its own identity is
        trusted.
        """
        client = getattr(websocket, "client", None)
        client_host = getattr(client, "host", None) if client else None
        presented = _token_from_ws(websocket)
        peer_uid = _peer_uid_from_ws(websocket)
        return self.authenticate(
            client_host=client_host,
            presented_token=presented,
            peer_uid=peer_uid,
            connection_id=connection_id,
        )

    # ── authorization ───────────────────────────────────────────────────────

    def authorize_write(self, identity: ConnectionIdentity | None) -> bool:
        """Server-side, delivery-time authorization for an answer / grant.

        v1: any authenticated operator identity may write. Evaluated at delivery
        time (not connect time) and against the server's own record of the
        connection identity, so a later seize/takeover phase can reject a
        deposed holder's in-flight answer here without changing call sites.
        """
        return bool(identity is not None and identity.authenticated)


def _safe_getuid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _token_from_ws(websocket) -> str | None:
    """Extract the presented token from a WebSocket (query param or header)."""
    params = getattr(websocket, "query_params", None)
    if params is not None:
        tok = params.get("token")
        if tok:
            return tok
    headers = getattr(websocket, "headers", None)
    if headers is not None:
        auth = headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip()
    return None


def _peer_uid_from_ws(websocket) -> int | None:
    """Best-effort peer UID for a UDS WebSocket.

    The raw peer socket is not exposed by Starlette's public surface, so this
    is a best-effort read of known scope locations. Returns ``None`` when the
    socket is unreachable — UDS security still holds via the owner-only run dir;
    the peer-cred UID is an audit-stamp / defense-in-depth read, and the
    per-OS resolution itself is exercised directly in the peer-cred unit test.
    """
    scope = getattr(websocket, "scope", None)
    if not isinstance(scope, dict):
        return None
    transport = scope.get("transport")
    sock = getattr(transport, "get_extra_info", None)
    if callable(sock):
        raw = transport.get_extra_info("socket")
        if raw is not None:
            try:
                return peer_uid_from_socket(raw)
            except OSError:
                return None
    return None


__all__ = [
    "OPERATOR_USER_ID",
    "TOKEN_ENV_VAR",
    "TransportTier",
    "ConnectionIdentity",
    "AuthContext",
    "AuthStartupError",
    "classify_transport",
    "is_loopback_host",
    "generate_token",
    "verify_token",
    "check_startup_binding",
]
