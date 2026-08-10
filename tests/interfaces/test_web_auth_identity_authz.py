"""Tier 2: connection identity + authorization for the tiered auth model.

ADR-0039 P0 invariants 1 + 3. Every connection CARRIES an identity, resolved
server-side (client-untrusted):

  - UDS: OS peer-cred gated — the operator's own UID authenticates; a foreign
    UID does not (defense in depth behind the socket file mode).
  - Loopback / network: a constant-time token compare authenticates; a missing
    or wrong token does not.
  - A concurrent T2+T3 bind resolves every authenticated connection to the SAME
    operator user-id, so authorization is uniform across tiers.

``authorize_write`` is the delivery-time write gate: only an authenticated
identity may answer / grant. Real :class:`AuthContext` instances throughout; no
mocks (there is no collaborator to fake — the logic is the unit).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.web.auth import (
    OPERATOR_USER_ID,
    AuthContext,
    TransportTier,
    provision_tls,
)

_SERVER_UID = 1000


def _ctx() -> AuthContext:
    return AuthContext(token="the-secret", server_uid=_SERVER_UID, require_token=True)


def test_uds_connection_with_operator_uid_authenticates_as_operator():
    """Tier 2: a UDS connection whose peer UID matches the server authenticates."""
    identity = _ctx().authenticate(
        client_host=None, presented_token=None, peer_uid=_SERVER_UID,
    )
    assert identity.authenticated is True
    assert identity.tier is TransportTier.UDS
    assert identity.user_id == OPERATOR_USER_ID
    assert identity.peer_uid == _SERVER_UID


def test_uds_connection_with_foreign_uid_is_rejected():
    """Tier 2: a UDS peer UID that differs from the server UID does NOT authenticate."""
    identity = _ctx().authenticate(
        client_host=None, presented_token=None, peer_uid=_SERVER_UID + 7,
    )
    assert identity.authenticated is False
    assert identity.user_id is None


def test_network_connection_requires_valid_token():
    """Tier 2: a network connection authenticates only with the correct token."""
    ctx = _ctx()
    good = ctx.authenticate(client_host="203.0.113.5", presented_token="the-secret")
    bad = ctx.authenticate(client_host="203.0.113.5", presented_token="wrong")
    missing = ctx.authenticate(client_host="203.0.113.5", presented_token=None)
    assert good.authenticated is True
    assert good.tier is TransportTier.NETWORK
    assert bad.authenticated is False
    assert missing.authenticated is False


def test_loopback_connection_requires_valid_token():
    """Tier 2: a loopback (browser) connection also needs the token (secure default)."""
    ctx = _ctx()
    good = ctx.authenticate(client_host="127.0.0.1", presented_token="the-secret")
    missing = ctx.authenticate(client_host="127.0.0.1", presented_token=None)
    assert good.authenticated is True
    assert good.tier is TransportTier.LOOPBACK
    assert missing.authenticated is False


def test_t2_and_t3_resolve_to_the_same_operator_user_id():
    """Tier 2: UDS (T2) and network (T3) authenticated identities share one user-id.

    A concurrent T2+T3 bind must apply the same authorization uniformly — the
    user-id is the authz anchor, and it is identical across tiers in v1.
    """
    ctx = _ctx()
    uds = ctx.authenticate(client_host=None, presented_token=None, peer_uid=_SERVER_UID)
    net = ctx.authenticate(client_host="198.51.100.9", presented_token="the-secret")
    authenticated_user_ids = {uds.user_id, net.user_id}
    assert authenticated_user_ids == {OPERATOR_USER_ID}


def test_authorize_write_gates_on_authentication():
    """Tier 2: only an authenticated identity may answer / grant (delivery-time gate)."""
    ctx = _ctx()
    auth_ok = ctx.authenticate(client_host="203.0.113.5", presented_token="the-secret")
    auth_no = ctx.authenticate(client_host="203.0.113.5", presented_token="wrong")
    assert ctx.authorize_write(auth_ok) is True
    assert ctx.authorize_write(auth_no) is False
    assert ctx.authorize_write(None) is False


def test_identity_audit_fields_carry_user_and_connection():
    """Tier 2: an identity exposes the user-id + connection + tier for audit stamping."""
    identity = _ctx().authenticate(
        client_host="203.0.113.5", presented_token="the-secret", connection_id="conn-xyz",
    )
    fields = identity.audit_fields()
    assert fields["auth_user_id"] == OPERATOR_USER_ID
    assert fields["auth_connection_id"] == "conn-xyz"
    assert fields["auth_tier"] == TransportTier.NETWORK.value


def test_self_signed_tls_material_has_fingerprint(tmp_path: Path):
    """Tier 2: T3 TLS provisioning yields a usable cert/key + a printable fingerprint."""
    material = provision_tls(tmp_path / "run")
    assert material.certfile.is_file()
    assert material.keyfile.is_file()
    # SHA-256 fingerprint = 32 bytes → 32 colon-separated hex pairs.
    assert material.fingerprint_sha256.count(":") == 31
