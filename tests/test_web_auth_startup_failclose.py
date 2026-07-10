"""Tier 2: fail-closed bind guard for the web gateway (ADR-0039 P0 invariant 2).

A non-loopback TCP bind WITHOUT a configured token is the accidental-exposure
hole (``reyn web --host 0.0.0.0`` with no auth would let any network client act
as the operator). :func:`check_startup_binding` must refuse to start it. UDS
binds (same-machine, socket file-mode gated) and loopback binds are allowed.

Falsification: if the guard stops raising on the network+no-token case, the
first test goes RED — proving the guard is load-bearing, not decorative.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.web.auth import AuthStartupError, check_startup_binding


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "10.0.0.1"])
def test_non_loopback_bind_without_token_refuses_to_start(host):
    """Tier 2: a non-loopback bind with no token raises AuthStartupError."""
    with pytest.raises(AuthStartupError):
        check_startup_binding(host, token=None, uds=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_loopback_bind_with_token_is_allowed(host):
    """Tier 2: a non-loopback bind WITH a configured token is allowed (T3 opt-in)."""
    # No exception = allowed to start.
    check_startup_binding(host, token="a-real-secret", uds=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", ""])
def test_loopback_bind_without_token_is_allowed(host):
    """Tier 2: a loopback bind is allowed without a token (same-machine surface)."""
    check_startup_binding(host, token=None, uds=False)


def test_uds_bind_without_token_is_allowed():
    """Tier 2: a UDS bind is allowed without a token (OS peer-cred / file-mode gated)."""
    check_startup_binding("0.0.0.0", token=None, uds=True)
