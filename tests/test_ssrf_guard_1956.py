"""Tier 2: reyn._ssrf_guard classifies internal / metadata IPs as denied (#1956 L2).

The single-source SSRF validator both clients call at every hop. Pure
classification for IP literals (no network); the ``allow_private`` opt-in and the
env-resolver are exercised directly. Tier line first.
"""
from __future__ import annotations

import pytest

from reyn._ssrf_guard import (
    SSRFBlocked,
    assert_fetch_host_allowed,
    resolve_allow_private,
)


def test_metadata_hard_deny_even_with_opt_in():
    """Tier 2: the cloud-metadata endpoint is denied even when allow_private=True
    (no opt-out — it must never be reachable)."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("169.254.169.254", allow_private=True)


def test_metadata_v6_hard_deny():
    """Tier 2: the AWS IMDSv6 endpoint (unique-local) is hard-denied despite
    being is_private — the explicit metadata set overrides the opt-in."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("fd00:ec2::254", allow_private=True)


def test_v4_mapped_metadata_cannot_bypass():
    """Tier 2: an IPv4-mapped IPv6 metadata address is normalised → denied."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("::ffff:169.254.169.254", allow_private=True)


def test_loopback_hard_deny():
    """Tier 2: loopback is denied even under the opt-in (no opt-out)."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("127.0.0.1", allow_private=True)


def test_link_local_denied():
    """Tier 2: a link-local address is denied."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("169.254.1.1", allow_private=False)


def test_private_denied_by_default():
    """Tier 2: private RFC1918 is denied by default (deny-private secure default)."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("10.0.0.1", allow_private=False)


def test_private_allowed_with_opt_in():
    """Tier 2: private RFC1918 is reachable ONLY via the operator opt-in."""
    assert_fetch_host_allowed("10.0.0.1", allow_private=True)  # no raise


def test_public_allowed():
    """Tier 2: a public IP literal is allowed (no over-block / regression)."""
    assert_fetch_host_allowed("8.8.8.8", allow_private=False)  # no raise


def test_empty_host_denied():
    """Tier 2: an empty host is denied."""
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("", allow_private=True)


def test_resolve_allow_private_reads_env_fail_secure(monkeypatch):
    """Tier 2: resolve_allow_private reads the config→env export; absent/false →
    False (fail-secure deny-private), a truthy value → True."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    assert resolve_allow_private() is False
    monkeypatch.setenv("REYN_FETCH_ALLOW_PRIVATE_IPS", "1")
    assert resolve_allow_private() is True
    monkeypatch.setenv("REYN_FETCH_ALLOW_PRIVATE_IPS", "false")
    assert resolve_allow_private() is False
