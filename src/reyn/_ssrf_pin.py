"""Connect-time IP-pinning for redirect-following HTTP clients (#1972 DNS-rebind).

Closes the check-time-vs-connect-time TOCTOU window: even if a fast-rebind
attacker changes the DNS answer between our validation call and the HTTP
client's connect() syscall, the client connects to the **pre-validated IP**
returned by ``resolve_and_validate`` at check time — not whatever the resolver
returns at connect time.

Two adapters are provided:

* **urllib** (stdlib ``http.client``): ``_PinnedHTTPConnection`` /
  ``_PinnedHTTPSConnection`` + their ``urllib.request`` handler wrappers
  ``_PinnedHTTPHandler`` / ``_PinnedHTTPSHandler``.  Add both handlers to a
  ``build_opener(...)`` call to get pinned urllib fetches.

* **httpx** (async): ``PinnedAsyncHTTPTransport`` — an
  ``httpx.AsyncHTTPTransport`` subclass that resolves + validates the host,
  rewrites ``request.url.host`` to the pinned IP, preserves the original
  ``Host`` header, and sets ``request.extensions["sni_hostname"]`` so httpcore
  validates the TLS cert against the HOSTNAME (not the raw IP).  Wire it with
  ``httpx.AsyncClient(transport=PinnedAsyncHTTPTransport(...))``.

Stdlib-only imports at module level (stdlib + ``reyn._ssrf_guard``) so this
module is importable from both ``reyn.api.*`` and ``reyn.core.*`` without
introducing a dependency cycle.  ``httpx`` is imported lazily inside
``PinnedAsyncHTTPTransport`` to keep the urllib path dependency-free.
"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import os
import socket
import urllib.request

from reyn import _ssrf_guard


def _resolve_allow_private() -> bool:
    """Operator opt-in for private-IP fetches (env-exported by config loader)."""
    return _ssrf_guard.resolve_allow_private()


def _ssrf_strict() -> bool:
    """``REYN_SSRF_STRICT`` opt-in (#3075 SSRF-x-proxy decision): when set truthy,
    a configured proxy is refused for reyn-originated SSRF-pinned egress and
    reyn's own target-IP pin is kept instead — the operator is choosing
    rebind-resistance over proxy routing for this class of request."""
    return os.environ.get("REYN_SSRF_STRICT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def ssrf_aware_client_kwargs(verify: bool | str = True) -> dict:
    """Build the ``httpx.AsyncClient`` ``transport=``/``mounts=`` kwargs that make
    an SSRF-pinned client honour the standard proxy env (#3075).

    **The decision** (issue #3075, "SSRF-pin x proxy"): ``PinnedAsyncHTTPTransport``
    pins each request to a pre-resolved, pre-validated IP — good DNS-rebind
    defense for a target httpx itself resolves, but structurally exclusive with a
    forward proxy: when a proxy is in play, the PROXY does the final target
    resolution/connect, not reyn, so there is no target IP for reyn to pin.

    Resolution (UX-first, tightening opt-in):

    * **No proxy configured** (no ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` for
      the request's scheme, honouring ``NO_PROXY``) — unchanged: every request
      goes through ``PinnedAsyncHTTPTransport(verify=verify)``, reyn's own
      resolve-validate-pin.
    * **Proxy configured, ``REYN_SSRF_STRICT`` unset** — the PROXY endpoint itself
      is SSRF-validated once at construction time (it is a fixed,
      operator-configured host, not LLM-supplied), then requests for that scheme
      are routed through ``httpx.AsyncHTTPTransport(proxy=..., verify=verify)``
      — final-target rebind protection is delegated to the proxy (its job: it is
      the one actually resolving + connecting to the target). ``NO_PROXY``
      exclusions still resolve direct through the pinned transport.
    * **Proxy configured, ``REYN_SSRF_STRICT`` truthy** — the proxy is refused for
      this SSRF-pinned client entirely; every request stays on
      ``PinnedAsyncHTTPTransport`` (reyn's own pin), matching the no-proxy case.
      An operator who needs rebind-resistance on a request stream they don't
      trust the corporate proxy for opts into this.

    Returns a dict suitable for ``httpx.AsyncClient(**ssrf_aware_client_kwargs(...))``
    (merge with the caller's other kwargs) — always contains ``"transport"``, and
    contains ``"mounts"`` only when a (non-strict) proxy was found.
    """
    default_transport = PinnedAsyncHTTPTransport(verify=verify)
    if _ssrf_strict():
        return {"transport": default_transport}

    from httpx._utils import get_environment_proxies

    env_proxies = get_environment_proxies()
    if not env_proxies:
        return {"transport": default_transport}

    import httpx

    mounts: dict[str, object] = {}
    for pattern, proxy_url in env_proxies.items():
        if proxy_url is None:
            # NO_PROXY exclusion pattern — explicit "no proxy for this pattern",
            # httpx represents this as a mount to None. Leave it None so httpx
            # falls back to the default transport (our pin) for excluded hosts.
            mounts[pattern] = None
            continue
        proxy_host = httpx.URL(proxy_url).host
        # The proxy endpoint is operator-configured (standard env, not
        # LLM-supplied), so a one-shot validation at construction time is the
        # right scope — not per-request re-pinning (the proxy resolves the
        # actual target on every request, which is the whole point of using it).
        _ssrf_guard.assert_fetch_host_allowed(
            proxy_host, allow_private=_resolve_allow_private()
        )
        mounts[pattern] = httpx.AsyncHTTPTransport(proxy=proxy_url, verify=verify)

    return {"transport": default_transport, "mounts": mounts}


# ── urllib (stdlib http.client) ───────────────────────────────────────────────


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """``http.client.HTTPConnection`` that connects to a pre-validated IP.

    ``self.host`` is PRESERVED (it carries the ``Host`` header); only the
    actual socket target is changed to the pinned IP returned by
    ``resolve_and_validate``.

    If ``resolve_and_validate`` returns ``[]`` (host unresolvable), we fall
    back to the normal ``super().connect()`` so the real DNS/connection error
    surfaces instead of a confusing SSRF error.
    """

    def connect(self) -> None:
        """Connect to the pinned IP while keeping ``self.host`` for the Host header."""
        import sys

        sys.audit("http.client.connect", self, self.host, self.port)
        ips = _ssrf_guard.resolve_and_validate(
            self.host, allow_private=_resolve_allow_private()
        )
        if not ips:
            # Unresolvable — fall back to the normal path; the OS will surface
            # the DNS error rather than us mislabelling it as SSRF-blocked.
            super().connect()
            return
        # Connect the socket to the pinned IP, preserving timeout / source_address
        # exactly as http.client.HTTPConnection.connect does — only the connect
        # target changes from (self.host, self.port) to (ips[0], self.port).
        self.sock = self._create_connection(
            (ips[0], self.port), self.timeout, self.source_address
        )
        # Mirror the TCP_NODELAY opt from the base class (ignore ENOPROTOOPT).
        import errno
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if e.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """``http.client.HTTPSConnection`` that connects to a pre-validated IP.

    The socket connects to the pinned IP, but TLS wrapping uses
    ``server_hostname=self.host`` (the ORIGINAL hostname) for SNI negotiation
    and certificate validation — so the server's certificate is validated
    against the hostname, not the numeric IP.

    Falls back to ``super().connect()`` when the host is unresolvable.
    """

    def connect(self) -> None:
        """Connect to pinned IP; TLS SNI + cert validation against original host."""
        import sys

        sys.audit("http.client.connect", self, self.host, self.port)
        ips = _ssrf_guard.resolve_and_validate(
            self.host, allow_private=_resolve_allow_private()
        )
        if not ips:
            super().connect()
            return

        import errno

        # Step 1 — TCP connect to the pinned IP (same timeout/source_address as base).
        sock = self._create_connection(
            (ips[0], self.port), self.timeout, self.source_address
        )
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if e.errno != errno.ENOPROTOOPT:
                raise

        # Step 2 — TLS wrap: use the connection's SSL context but override
        # server_hostname to the ORIGINAL hostname (SNI + cert validation).
        # Mirrors http.client.HTTPSConnection.connect — _tunnel_host takes
        # priority for CONNECT-tunnelled requests (proxy), just like the base.
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            server_hostname: str = self._tunnel_host
        else:
            server_hostname = self.host

        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """``urllib.request.HTTPHandler`` that uses ``_PinnedHTTPConnection``."""

    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """``urllib.request.HTTPSHandler`` that uses ``_PinnedHTTPSConnection``.

    Preserves the parent's SSL context (``self._context``) so any custom CA
    bundle / check_hostname / verify_mode from the existing opener is honoured.
    """

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


# ── httpx (async) ─────────────────────────────────────────────────────────────


class PinnedAsyncHTTPTransport:
    """Async HTTP transport that pins each request to a pre-validated IP
    (#1972 full DNS-rebind resistance).

    Wraps an ``httpx.AsyncHTTPTransport`` and implements the
    ``httpx.AsyncBaseTransport`` protocol (``__aenter__`` / ``__aexit__`` /
    ``aclose`` / ``handle_async_request``) so it can be passed directly as
    ``httpx.AsyncClient(transport=...)``.

    For each request:

    1. If ``request.url.host`` is already a bare IP literal — validate it via
       ``assert_fetch_host_allowed`` (no DNS involved) and pass through to the
       underlying transport unchanged.

    2. Otherwise — resolve + validate via ``resolve_and_validate`` (thread-
       pool, to keep the event loop unblocked).  If the result is empty
       (unresolvable host) — pass through to the underlying transport, which
       will surface the real DNS / connection error.

    3. On a valid pin: rewrite ``request.url.host`` to ``ips[0]`` so the
       socket connects to the pinned IP; inject the original authority into
       ``request.headers["Host"]`` (so the server sees the right hostname);
       and set ``request.extensions["sni_hostname"]`` to the original hostname
       (httpcore reads this extension and passes it to ``start_tls`` as
       ``server_hostname``, enabling cert validation + SNI against the hostname
       not the IP).

    Because httpx calls the transport per-hop in a manual redirect loop,
    every redirect hop is independently validated and pinned.

    Constructor mirrors ``httpx.AsyncHTTPTransport(verify=...)`` — pass the
    same ``verify`` value you'd give ``httpx.AsyncClient``.
    """

    def __init__(self, verify: bool | str = True) -> None:
        import httpx

        self._transport = httpx.AsyncHTTPTransport(verify=verify)

    # ── async context manager protocol (required by httpx.AsyncClient) ────────

    async def __aenter__(self) -> "PinnedAsyncHTTPTransport":
        await self._transport.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._transport.__aexit__(*args)

    async def aclose(self) -> None:
        await self._transport.aclose()

    # ── core transport method ─────────────────────────────────────────────────

    async def handle_async_request(self, request) -> object:
        """Validate + pin the request's host, then delegate to the transport."""
        import httpx

        host: str = request.url.host

        # ── bare IP literal path ──────────────────────────────────────────────
        try:
            ipaddress.ip_address(host)
            is_literal = True
        except ValueError:
            is_literal = False

        if is_literal:
            # Validate but no re-resolution needed — it IS the connect target.
            _ssrf_guard.assert_fetch_host_allowed(
                host, allow_private=_resolve_allow_private()
            )
            return await self._transport.handle_async_request(request)

        # ── hostname path ─────────────────────────────────────────────────────
        ips = await asyncio.to_thread(
            _ssrf_guard.resolve_and_validate,
            host,
            allow_private=_resolve_allow_private(),
        )
        if not ips:
            # Unresolvable — pass through; transport will surface DNS error.
            return await self._transport.handle_async_request(request)

        pin = ips[0]

        # Build the original Host authority string (host + non-default port).
        # For standard ports (80/443) httpx returns port=None; in that case
        # we omit the port from the Host header (RFC 7230 §5.4).
        port = request.url.port
        scheme = request.url.scheme
        standard_port = (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        )
        if port is None or standard_port:
            original_authority = host
        else:
            original_authority = f"{host}:{port}"

        # Rewrite the URL host to the pinned IP (socket connect target).
        new_url = request.url.copy_with(host=pin)

        # sni_hostname must be bytes for httpcore (it decodes the extension in
        # _connect).  str is also accepted in practice, but bytes is the
        # canonical form used throughout httpcore's own test fixtures.
        sni_bytes = host.encode("ascii")

        # Build a new request with the pinned URL, overridden Host header,
        # and the sni_hostname extension.  httpx.Request is constructed from
        # scratch (immutable-ish) — we copy all fields and replace what's needed.
        headers = list(request.headers.raw)
        # Replace or insert the Host header (case-insensitive search).
        host_key = b"host"
        new_headers = [
            (k, v) for k, v in headers if k.lower() != host_key
        ]
        new_headers.insert(0, (b"host", original_authority.encode("ascii")))

        pinned_request = httpx.Request(
            method=request.method,
            url=new_url,
            headers=new_headers,
            content=request.content,
            extensions={**request.extensions, "sni_hostname": sni_bytes},
        )

        return await self._transport.handle_async_request(pinned_request)
