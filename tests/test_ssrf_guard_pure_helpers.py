"""Tier 2: pure helpers in _ssrf_guard.py — normalise and deny_reason.

  ``_normalise(ip)``  — collapses IPv4-mapped IPv6 to its IPv4 form
  ``_deny_reason(ip, allow_private)`` — returns a deny-reason string or None
"""
from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn._ssrf_guard import _deny_reason, _normalise


def _ip4(s: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(s)


def _ip6(s: str) -> ipaddress.IPv6Address:
    return ipaddress.IPv6Address(s)


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------


def test_normalise_plain_ipv4_passthrough() -> None:
    """Tier 2: plain IPv4 address is returned unchanged."""
    ip = _ip4("8.8.8.8")
    assert _normalise(ip) == ip


def test_normalise_plain_ipv6_passthrough() -> None:
    """Tier 2: non-mapped IPv6 address is returned unchanged."""
    ip = _ip6("2001:db8::1")
    assert _normalise(ip) == ip


def test_normalise_ipv4_mapped_ipv6_collapses_to_ipv4() -> None:
    """Tier 2: ::ffff:x.x.x.x collapses to the IPv4 form."""
    mapped = _ip6("::ffff:192.168.1.1")
    result = _normalise(mapped)
    assert isinstance(result, ipaddress.IPv4Address)
    assert result == _ip4("192.168.1.1")


def test_normalise_ipv4_mapped_metadata_collapses() -> None:
    """Tier 2: ::ffff:169.254.169.254 collapses to the metadata IPv4."""
    mapped = _ip6("::ffff:169.254.169.254")
    result = _normalise(mapped)
    assert isinstance(result, ipaddress.IPv4Address)
    assert result == _ip4("169.254.169.254")


# ---------------------------------------------------------------------------
# _deny_reason
# ---------------------------------------------------------------------------


def test_deny_reason_public_ip_allowed() -> None:
    """Tier 2: public routable IP has no deny reason."""
    assert _deny_reason(_ip4("8.8.8.8"), allow_private=False) is None


def test_deny_reason_metadata_ipv4() -> None:
    """Tier 2: cloud-metadata 169.254.169.254 is always denied."""
    result = _deny_reason(_ip4("169.254.169.254"), allow_private=True)
    assert result is not None
    assert "metadata" in result


def test_deny_reason_metadata_ipv6() -> None:
    """Tier 2: cloud-metadata fd00:ec2::254 is always denied."""
    result = _deny_reason(_ip6("fd00:ec2::254"), allow_private=True)
    assert result is not None
    assert "metadata" in result


def test_deny_reason_loopback_ipv4() -> None:
    """Tier 2: 127.0.0.1 is denied as loopback."""
    result = _deny_reason(_ip4("127.0.0.1"), allow_private=False)
    assert result is not None
    assert "loopback" in result


def test_deny_reason_loopback_ipv6() -> None:
    """Tier 2: ::1 is denied as loopback."""
    result = _deny_reason(_ip6("::1"), allow_private=False)
    assert result is not None
    assert "loopback" in result


def test_deny_reason_link_local_non_metadata() -> None:
    """Tier 2: link-local address (not metadata) is denied as link-local."""
    result = _deny_reason(_ip4("169.254.0.1"), allow_private=False)
    assert result is not None
    assert "link-local" in result


def test_deny_reason_multicast() -> None:
    """Tier 2: multicast address is denied."""
    result = _deny_reason(_ip4("224.0.0.1"), allow_private=False)
    assert result is not None
    assert "multicast" in result


def test_deny_reason_private_denied_when_not_allowed() -> None:
    """Tier 2: RFC1918 private IP is denied when allow_private=False."""
    result = _deny_reason(_ip4("192.168.1.1"), allow_private=False)
    assert result is not None
    assert "private" in result.lower() or "RFC1918" in result


def test_deny_reason_private_allowed_when_flag_set() -> None:
    """Tier 2: RFC1918 private IP is allowed when allow_private=True."""
    assert _deny_reason(_ip4("192.168.1.1"), allow_private=True) is None


def test_deny_reason_ipv4_mapped_metadata_denied() -> None:
    """Tier 2: IPv4-mapped metadata address is denied after normalisation."""
    mapped = _ip6("::ffff:169.254.169.254")
    result = _deny_reason(mapped, allow_private=True)
    assert result is not None
    assert "metadata" in result
