"""Tier 2: connect-time IP-pinning closes the DNS-rebind TOCTOU (#1972).

Each test uses a real recording seam (function or subclass, NOT MagicMock) to
capture the connect target / request seen by the network layer, then asserts on
the *public behavior under test*: the socket connects to the pre-validated IP,
not to whatever the OS resolver would return at connect time.

Testing policy compliance:
  - Tier 2 (OS invariant) — the pinned-IP guarantee is a security invariant of
    the OS's outbound-fetch path.
  - No MagicMock / AsyncMock / patch used anywhere; real recording stand-ins only.
  - No private-state assertions; we assert on the recorded connect target (= the
    externally observable behavior of the socket layer).
"""
from __future__ import annotations

import asyncio
import http.client
import socket
from typing import Any

import pytest

from reyn._ssrf_guard import SSRFBlocked
from reyn._ssrf_pin import (
    PinnedAsyncHTTPTransport,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
)

# ── Shared helper ──────────────────────────────────────────────────────────────

PUBLIC_IP_A = "93.184.216.34"   # example.com — a real, publicly routable IP
PUBLIC_IP_B = "8.8.8.8"         # alternate public IP


def _fake_resolve_and_validate(*ips: str):
    """Real function (NOT a mock) that records calls and returns fixed IPs.

    Returns a callable matching the ``resolve_and_validate(host, *, allow_private)``
    signature.  The returned call log (a plain list) lets tests assert that
    the exact IP used for the connect target came from the resolve-at-check-time
    call, not from a separate OS resolution.
    """
    calls: list[tuple[str, bool]] = []

    def _fn(host: str, *, allow_private: bool) -> list[str]:
        calls.append((host, allow_private))
        if not ips:
            return []
        return list(ips)

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


# ── urllib: _PinnedHTTPConnection ─────────────────────────────────────────────


class _RecordingHTTPConnection(_PinnedHTTPConnection):
    """_PinnedHTTPConnection subclass that records the (ip, port) passed to
    ``_create_connection`` and raises ConnectionRefusedError to avoid a real
    network call.  This is a real class (no mocks); the recorded target is
    the single assertion point.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded_target: tuple[str, int] | None = None
        # Replace the instance-level _create_connection with a real recording fn.
        _outer = self

        def _recorder(address: tuple[str, int], timeout: Any, source_address: Any):
            _outer.recorded_target = address
            raise ConnectionRefusedError("recording stand-in — no real network")

        self._create_connection = _recorder


def test_pinned_http_connection_uses_validated_ip(monkeypatch):
    """Tier 2: _PinnedHTTPConnection.connect() passes the pre-validated IP (not
    the hostname) to _create_connection — proving no connect-time re-resolve."""
    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    conn = _RecordingHTTPConnection("example.com", 80)
    with pytest.raises(ConnectionRefusedError):
        conn.connect()

    # The socket was asked to connect to the pinned IP, not "example.com".
    assert conn.recorded_target == (PUBLIC_IP_A, 80)
    # The Host header target (self.host) is UNCHANGED — preserves Host header.
    assert conn.host == "example.com"


def test_pinned_http_connection_fallback_when_unresolvable(monkeypatch):
    """Tier 2: when resolve_and_validate returns [] (unresolvable), fall back to
    super().connect() — the OS will surface the DNS error, not an SSRF block."""
    fake_rv = _fake_resolve_and_validate()  # empty → unresolvable
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    conn = http.client.HTTPConnection("nx-does-not-exist.invalid", 80)
    # We can't easily intercept super().connect() without a mock, but we can
    # confirm that _PinnedHTTPConnection does NOT raise SSRFBlocked — it
    # raises the expected connection/DNS error from the OS path instead.
    conn2 = _RecordingHTTPConnection("nx-does-not-exist.invalid", 80)
    # Override resolve to return [] — fallback fires super().connect(), which
    # hits the real OS and raises socket.gaierror (or similar), NOT SSRFBlocked.
    with pytest.raises((OSError, ConnectionRefusedError)):
        conn2.connect()  # recorded_target stays None (super path was taken)


def test_pinned_http_connection_blocked_host_raises(monkeypatch):
    """Tier 2: resolve_and_validate raising SSRFBlocked propagates out of
    _PinnedHTTPConnection.connect() — the blocked target is never connected."""
    def _deny(host: str, *, allow_private: bool) -> list[str]:
        raise SSRFBlocked(f"blocked: {host}")

    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", _deny)

    conn = _RecordingHTTPConnection("metadata.internal", 80)
    with pytest.raises(SSRFBlocked):
        conn.connect()
    # No connect call was recorded — the block happened before the socket layer.
    assert conn.recorded_target is None


# ── urllib: _PinnedHTTPSConnection ────────────────────────────────────────────


class _RecordingHTTPSConnection(_PinnedHTTPSConnection):
    """_PinnedHTTPSConnection subclass that records (ip, port) + the SNI hostname
    passed to wrap_socket, then raises to avoid a real TLS handshake.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded_target: tuple[str, int] | None = None
        self.recorded_server_hostname: str | None = None
        _outer = self

        def _recorder(address: tuple[str, int], timeout: Any, source_address: Any):
            _outer.recorded_target = address
            # Return a real socket object so wrap_socket has something to work
            # with — except we intercept wrap_socket too (below).
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            return sock

        self._create_connection = _recorder

        # Also intercept wrap_socket on the SSL context to capture server_hostname.
        original_wrap = self._context.wrap_socket

        def _recording_wrap(s, *, server_hostname=None, **kw):
            _outer.recorded_server_hostname = server_hostname
            # Close the real socket — we don't need the TLS layer for this test.
            try:
                s.close()
            except Exception:
                pass
            raise ConnectionRefusedError("recording stand-in — no real TLS")

        self._context.wrap_socket = _recording_wrap  # type: ignore[method-assign]


def test_pinned_https_connection_pins_ip_keeps_host_for_sni(monkeypatch):
    """Tier 2: _PinnedHTTPSConnection.connect() connects the socket to the pinned
    IP and uses the ORIGINAL hostname for TLS SNI / cert validation — not the IP.
    This is the core DNS-rebind TOCTOU property: connect target ≠ cert target."""
    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    conn = _RecordingHTTPSConnection("example.com", 443)
    with pytest.raises(ConnectionRefusedError):
        conn.connect()

    # Socket connected to the pinned IP, not "example.com".
    assert conn.recorded_target == (PUBLIC_IP_A, 443)
    # TLS SNI / cert validation used the original hostname — not the IP.
    assert conn.recorded_server_hostname == "example.com"
    # self.host is unchanged — so the Host header stays the hostname.
    assert conn.host == "example.com"


def test_dns_rebind_proof_https(monkeypatch):
    """Tier 2: DNS-rebind resistance proof — resolve_and_validate returns IP_A at
    check time; the socket connects to IP_A regardless of what a subsequent OS
    DNS call would return.  No second resolution happens because the pinned IP
    is used directly for the socket.  Verifies: recorded_target == IP_A."""
    # Simulate attacker rebinding: the fake returns IP_A (public) the first time
    # (check-time); if there were a second OS call it would return "rebind" IP.
    # Because _PinnedHTTPSConnection calls resolve_and_validate ONCE and passes
    # ips[0] directly to _create_connection, there is no second OS call.
    call_count = 0

    def _rebind_resolver(host: str, *, allow_private: bool) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [PUBLIC_IP_A]   # check-time answer: public/valid
        # If called again (= connect-time re-resolve), attacker's answer.
        return ["169.254.169.254"]

    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", _rebind_resolver)

    conn = _RecordingHTTPSConnection("rebind.example", 443)
    with pytest.raises(ConnectionRefusedError):
        conn.connect()

    # Exactly ONE resolution call — the check-time call.  No connect-time re-resolve.
    assert call_count == 1
    # The socket targeted the check-time IP, not whatever rebind would return.
    assert conn.recorded_target == (PUBLIC_IP_A, 443)


# ── httpx: PinnedAsyncHTTPTransport ───────────────────────────────────────────


class _RecordingTransport(PinnedAsyncHTTPTransport):
    """PinnedAsyncHTTPTransport subclass that records the request handed to the
    underlying network layer instead of making a real connection.

    This is a real class override (NOT a mock): we override
    ``_transport.handle_async_request`` with a recording coroutine that captures
    the request and returns a minimal ``httpx.Response``.
    """

    def __init__(self) -> None:
        import httpx
        super().__init__(verify=False)
        self.recorded_request: httpx.Request | None = None
        _outer = self

        async def _recording_handler(request: httpx.Request) -> httpx.Response:
            _outer.recorded_request = request
            return httpx.Response(200, content=b"ok")

        # Replace the inner transport's handle_async_request with our recorder.
        self._transport.handle_async_request = _recording_handler  # type: ignore[method-assign]


def _run(coro):
    return asyncio.run(coro)


def test_pinned_transport_rewrites_url_host_to_ip(monkeypatch):
    """Tier 2: PinnedAsyncHTTPTransport rewrites request.url.host → the validated
    IP before passing to the underlying transport — the socket layer sees the IP,
    not the hostname (no connect-time re-resolve)."""
    import httpx

    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://example.com/path")

    _run(transport.handle_async_request(original_req))

    assert transport.recorded_request is not None
    # URL host was rewritten to the pinned IP.
    assert transport.recorded_request.url.host == PUBLIC_IP_A


def test_pinned_transport_preserves_host_header(monkeypatch):
    """Tier 2: PinnedAsyncHTTPTransport sets Host = original hostname (not the
    pinned IP), so the server receives the correct virtual-host name."""
    import httpx

    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://example.com/path")

    _run(transport.handle_async_request(original_req))

    assert transport.recorded_request is not None
    host_header = transport.recorded_request.headers.get("host")
    assert host_header == "example.com"


def test_pinned_transport_sets_sni_hostname_extension(monkeypatch):
    """Tier 2: PinnedAsyncHTTPTransport sets extensions['sni_hostname'] to the
    original hostname bytes so httpcore uses it for TLS SNI + cert validation
    (not the numeric IP that the socket connects to)."""
    import httpx

    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://example.com/path")

    _run(transport.handle_async_request(original_req))

    assert transport.recorded_request is not None
    sni = transport.recorded_request.extensions.get("sni_hostname")
    assert sni == b"example.com"


def test_pinned_transport_non_standard_port_host_header(monkeypatch):
    """Tier 2: non-standard port included in Host header (e.g. example.com:8443)."""
    import httpx

    fake_rv = _fake_resolve_and_validate(PUBLIC_IP_A)
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://example.com:8443/path")

    _run(transport.handle_async_request(original_req))

    assert transport.recorded_request is not None
    host_header = transport.recorded_request.headers.get("host")
    assert host_header == "example.com:8443"


def test_pinned_transport_bare_ip_literal_passes_through(monkeypatch):
    """Tier 2: a bare-IP-literal URL (already the connect target) passes through
    unchanged after assert_fetch_host_allowed — no URL rewrite, no sni_hostname."""
    import httpx

    # assert_fetch_host_allowed must be called (L2 check), not resolve_and_validate
    checked: list[str] = []

    def _assert(host: str, *, allow_private: bool) -> None:
        checked.append(host)

    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.assert_fetch_host_allowed", _assert)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", f"https://{PUBLIC_IP_A}/path")

    _run(transport.handle_async_request(original_req))

    # The IP was validated.
    assert PUBLIC_IP_A in checked
    # URL host is unchanged (still the IP).
    assert transport.recorded_request is not None
    assert transport.recorded_request.url.host == PUBLIC_IP_A
    # No sni_hostname injected for bare-IP requests.
    assert "sni_hostname" not in transport.recorded_request.extensions


def test_pinned_transport_unresolvable_falls_through(monkeypatch):
    """Tier 2: when resolve_and_validate returns [] the transport passes the
    original request to the underlying layer (which surfaces the DNS error)."""
    import httpx

    fake_rv = _fake_resolve_and_validate()  # empty → unresolvable
    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", fake_rv)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://nx.invalid/path")

    _run(transport.handle_async_request(original_req))

    assert transport.recorded_request is not None
    # URL host is NOT rewritten — the original request passes through.
    assert transport.recorded_request.url.host == "nx.invalid"


def test_pinned_transport_blocked_host_raises(monkeypatch):
    """Tier 2: resolve_and_validate raising SSRFBlocked propagates out of
    PinnedAsyncHTTPTransport — the request never reaches the transport."""
    import httpx

    def _deny(host: str, *, allow_private: bool) -> list[str]:
        raise SSRFBlocked(f"blocked: {host}")

    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", _deny)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://internal.example/path")

    with pytest.raises(SSRFBlocked):
        _run(transport.handle_async_request(original_req))

    # No request reached the recording layer.
    assert transport.recorded_request is None


def test_dns_rebind_proof_httpx(monkeypatch):
    """Tier 2: DNS-rebind resistance proof for httpx — resolve_and_validate is
    called exactly ONCE (check time); the URL host is rewritten to the check-time
    IP so no connect-time re-resolve can deliver a different (attacker) IP."""
    import httpx

    call_count = 0

    def _rebind_resolver(host: str, *, allow_private: bool) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [PUBLIC_IP_A]   # check-time answer
        return ["169.254.169.254"]  # attacker's rebind answer

    monkeypatch.setattr("reyn._ssrf_pin._ssrf_guard.resolve_and_validate", _rebind_resolver)

    transport = _RecordingTransport()
    original_req = httpx.Request("GET", "https://rebind.example/path")

    _run(transport.handle_async_request(original_req))

    # Exactly one resolve call — no connect-time re-resolve.
    assert call_count == 1
    assert transport.recorded_request is not None
    # The socket target is the check-time IP (not what a second call would return).
    assert transport.recorded_request.url.host == PUBLIC_IP_A
