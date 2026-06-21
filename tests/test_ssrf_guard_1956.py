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
    resolve_and_validate,
)


def _fake_getaddrinfo(*ips: str):
    """A real getaddrinfo stand-in (NOT a mock) returning fixed IPs — the seam
    for resolution + DNS-rebind tests."""
    import socket as _s

    def _f(host, port, **kw):
        return [(_s.AF_INET, _s.SOCK_STREAM, _s.IPPROTO_TCP, "", (ip, 0)) for ip in ips]

    return _f


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


# ── resolve_and_validate (#1972 — the pinned-IP source) ──────────────────────


def test_resolve_and_validate_literal_returns_self():
    """Tier 2: a public IP literal is returned as the pinned connect target
    (no DNS — it IS the target)."""
    assert resolve_and_validate("93.184.216.34", allow_private=False) == ["93.184.216.34"]


def test_resolve_and_validate_literal_denied_raises():
    """Tier 2: a denied literal (metadata) raises rather than returning a pin."""
    with pytest.raises(SSRFBlocked):
        resolve_and_validate("169.254.169.254", allow_private=False)


def test_resolve_and_validate_hostname_returns_validated_ips(monkeypatch):
    """Tier 2: a hostname resolves to its validated public IPs (the pin set),
    de-duplicated in resolution order — real getaddrinfo seam, no mock."""
    monkeypatch.setattr(
        "reyn._ssrf_guard.socket.getaddrinfo",
        _fake_getaddrinfo("93.184.216.34", "93.184.216.34", "8.8.8.8"),
    )
    assert resolve_and_validate("example.com", allow_private=False) == [
        "93.184.216.34", "8.8.8.8",
    ]


def test_resolve_and_validate_denies_if_any_ip_internal(monkeypatch):
    """Tier 2: a hostname resolving to ANY denied IP raises (deny-if-any) — the
    rebind-relevant answer with a public AND an internal IP is rejected, so the
    caller never pins to (or falls back past) a poisoned record."""
    monkeypatch.setattr(
        "reyn._ssrf_guard.socket.getaddrinfo",
        _fake_getaddrinfo("93.184.216.34", "169.254.169.254"),
    )
    with pytest.raises(SSRFBlocked):
        resolve_and_validate("rebind.example", allow_private=False)


def test_resolve_and_validate_unresolvable_returns_empty(monkeypatch):
    """Tier 2: an unresolvable host returns [] (no IP to pin; the caller falls
    back to its normal connect, which surfaces the real DNS error) — non-gating,
    matching assert_fetch_host_allowed."""
    import socket as _s

    def _raise(*a, **k):
        raise _s.gaierror("no such host")

    monkeypatch.setattr("reyn._ssrf_guard.socket.getaddrinfo", _raise)
    assert resolve_and_validate("nx.invalid", allow_private=False) == []


def test_assert_delegates_to_resolve_and_validate(monkeypatch):
    """Tier 2: assert_fetch_host_allowed delegates to resolve_and_validate — a
    hostname resolving to an internal IP raises via the shared resolve path."""
    monkeypatch.setattr(
        "reyn._ssrf_guard.socket.getaddrinfo", _fake_getaddrinfo("10.0.0.5"),
    )
    with pytest.raises(SSRFBlocked):
        assert_fetch_host_allowed("internal.example", allow_private=False)
