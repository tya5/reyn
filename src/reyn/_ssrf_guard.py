"""Single-source SSRF guard — deny outbound fetches resolving to internal IPs.

#1956: ``web_fetch`` / ``safe.http`` validated only the INITIAL host against the
declared allowlist, then followed HTTP redirects transparently — so an
allowlisted host could redirect to a link-local / metadata / loopback / private
target (e.g. the cloud metadata endpoint ``169.254.169.254`` → IAM creds into
the LLM context). This module is the single-source **Layer 2** validator applied
at EVERY host gate (initial request + each redirect hop) across all
redirect-following clients. **Layer 1** (per-hop allowlist re-validation) lives
in each client.

Stdlib-only leaf (``socket`` / ``ipaddress`` / ``os``) so it imports cleanly from
both ``reyn.api.*`` and ``reyn.core.*`` with no reyn import cycle — mirrors
:mod:`reyn._http_limits`.

Policy (lead-approved, #1956):
  - **HARD deny, no opt-out** — link-local (``169.254.0.0/16``, ``fe80::/10``),
    cloud-metadata (``169.254.169.254``, ``fd00:ec2::254``), loopback
    (``127.0.0.0/8``, ``::1``), reserved, multicast, unspecified. An LLM/skill
    fetch has no legitimate use for these.
  - **deny by default, operator opt-in** — private RFC1918 / ULA
    (``10/8``, ``172.16/12``, ``192.168/16``, ``fc00::/7``). Allowed only when
    ``allow_private`` is True (``web.fetch.allow_private_ips: true``, for
    enterprise internal-fetch).

Resolution is DNS-aware: the host is resolved and EVERY returned IP is checked
(deny if ANY is denied), so an allowlisted hostname that resolves to an internal
IP is caught. IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is normalised so a mapped
metadata/internal address cannot bypass. Residual: the client re-resolves at
connect (a small TOCTOU window vs full IP-pinning) — tracked separately as
future hardening.
"""
from __future__ import annotations

import ipaddress
import os
import socket

_IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address

# Cloud metadata endpoints (IMDS). Hard-deny even though ``fd00:ec2::254`` is
# unique-local (= ``is_private``): metadata is never a legitimate fetch target,
# so it must be denied regardless of the ``allow_private`` opt-in.
_METADATA_IPS: frozenset[_IPAddr] = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure IMDS (v4)
    ipaddress.ip_address("fd00:ec2::254"),      # AWS IMDS (v6)
})

# Config→env export read by config-less surfaces (the safe.http subprocess +
# the registry main-process modules), mirroring REYN_MCP_REGISTRY_URLS. Absent
# / unset → deny-private (fail-secure even if a sandbox strips the env).
_ALLOW_PRIVATE_ENV = "REYN_FETCH_ALLOW_PRIVATE_IPS"

# Shared redirect cap so both manual-loop clients agree (httpx default is 20,
# urllib's is 10 — single-source the bound here).
MAX_REDIRECTS = 20


class SSRFBlocked(PermissionError):
    """Raised when an outbound fetch target resolves to a denied (internal) IP."""


def _normalise(ip: _IPAddr) -> _IPAddr:
    """Collapse an IPv4-mapped IPv6 address to its IPv4 form (bypass guard)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _deny_reason(ip: _IPAddr, *, allow_private: bool) -> str | None:
    """Return a human deny-reason if ``ip`` is disallowed, else ``None``."""
    ip = _normalise(ip)
    if ip in _METADATA_IPS:
        return "cloud-metadata endpoint"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved or ip.is_unspecified:
        return "reserved"
    # ``is_private`` also covers loopback/link-local for IPv4, but those are
    # caught above with specific reasons; this is the RFC1918 / ULA case.
    if ip.is_private and not allow_private:
        return "private (RFC1918/ULA)"
    return None


def assert_fetch_host_allowed(host: str, *, allow_private: bool) -> None:
    """Raise :class:`SSRFBlocked` if ``host`` resolves to any denied IP.

    A bare IP literal is checked directly (the common redirect-to-IP attack).
    A hostname is resolved and EVERY returned address is checked (deny if ANY is
    denied). An unresolvable host is left to the client's own connection error
    (nothing to gate — no reachable IP). Empty host is denied.
    """
    if not host:
        raise SSRFBlocked("blocked fetch: empty host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _deny_reason(literal, allow_private=allow_private)
        if reason is not None:
            raise SSRFBlocked(f"blocked fetch to {host} ({reason})")
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable here → no IP to gate; let the client surface its own
        # DNS/connection failure rather than mislabelling it as an SSRF block.
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        reason = _deny_reason(ip, allow_private=allow_private)
        if reason is not None:
            raise SSRFBlocked(f"blocked fetch to {host} → {ip} ({reason})")


def resolve_allow_private() -> bool:
    """Operator opt-in for private-IP fetches, from the config→env export.

    The config loader exports ``web.fetch.allow_private_ips`` into
    ``REYN_FETCH_ALLOW_PRIVATE_IPS``; the safe.http subprocess and the
    config-less registry main-process modules read it here. Absent / unset →
    ``False`` (deny private — fail-secure even if a sandbox strips the env).
    """
    return os.environ.get(_ALLOW_PRIVATE_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )
