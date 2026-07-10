"""Server-side authentication for the Reyn web / thin-client gateway.

The gateway's answer and permission-grant paths are gated behind a tiered,
secure-by-default authentication model: every connection carries an identity
(:mod:`reyn.interfaces.web.auth.core`), same-machine UDS connections are
identified by their OS peer credentials (:mod:`reyn.interfaces.web.auth.peercred`),
and cross-machine binds require a token over self-signed TLS
(:mod:`reyn.interfaces.web.auth.tls`). A non-loopback bind without a token
refuses to start (fail-closed).
"""
from __future__ import annotations

from reyn.interfaces.web.auth.core import (
    OPERATOR_USER_ID,
    TOKEN_ENV_VAR,
    AuthContext,
    AuthStartupError,
    ConnectionIdentity,
    TransportTier,
    check_startup_binding,
    classify_transport,
    generate_token,
    is_loopback_host,
    verify_token,
)
from reyn.interfaces.web.auth.peercred import peer_uid_from_socket
from reyn.interfaces.web.auth.tls import (
    TlsMaterial,
    TlsProvisioningError,
    provision_tls,
)

__all__ = [
    "OPERATOR_USER_ID",
    "TOKEN_ENV_VAR",
    "AuthContext",
    "AuthStartupError",
    "ConnectionIdentity",
    "TransportTier",
    "check_startup_binding",
    "classify_transport",
    "generate_token",
    "is_loopback_host",
    "verify_token",
    "peer_uid_from_socket",
    "TlsMaterial",
    "TlsProvisioningError",
    "provision_tls",
]
