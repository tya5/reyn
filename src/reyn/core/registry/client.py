"""Async HTTP client for the MCP server registry.

Public API:
  ``RegistryClient.search(query, limit)  -> list[ServerInfo]``
  ``RegistryClient.get_server(name)      -> ServerJson``

Base URL is resolved from the ``REYN_MCP_REGISTRY_URL`` environment variable,
defaulting to ``https://registry.modelcontextprotocol.io``.

Cache strategy:
  Results are cached in ``~/.reyn/registry-cache/`` (TTL 24h).  On network
  failure the client raises ``RegistryError``; callers that want stale-data
  fallback should catch the exception and call ``cache.get()`` directly.

Endpoints consumed (v0.1, preview — schema may change):
  ``GET /v0.1/servers?search=<query>&limit=<n>``
  ``GET /v0.1/servers/<name>/versions/latest``

The ``name`` in the second endpoint is the registry identifier as returned
by the search response (e.g. ``"io.github.foo/bar-mcp"``).
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import TYPE_CHECKING

from reyn import _ssrf_guard

if TYPE_CHECKING:
    pass


async def _ssrf_request_hook(request) -> None:
    """#1956 SSRF: gate EVERY httpx request — the initial AND each redirect hop
    (httpx request event-hooks fire per-hop, verified) — against the IP-deny
    guard, so a malicious / compromised registry that redirects to an internal
    IP is blocked. ``allow_private`` is the operator opt-in (env-exported)."""
    await asyncio.to_thread(
        _ssrf_guard.assert_fetch_host_allowed,
        request.url.host or "",
        allow_private=_ssrf_guard.resolve_allow_private(),
    )


class RegistryError(Exception):
    """Raised when the registry is unreachable or returns an error."""


def _parse_semver(version: str) -> tuple[int, ...]:
    """Parse a semver string into a comparable tuple of ints.

    Non-numeric segments are treated as 0 so malformed versions don't crash.
    Returns ``(0,)`` for empty or unparseable input.
    """
    parts = []
    for segment in version.split(".")[:3]:
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _dedup_by_latest(raw_entries: list[dict]) -> list[dict]:
    """Deduplicate registry search entries by server name.

    When the registry returns multiple version entries for the same server
    name, keep only the best one using this priority:
      1. Entry where ``_meta…isLatest`` is ``true``.
      2. Entry with the highest semver ``server.version``.
      3. Last-seen entry (preserve original list order as tiebreaker).

    Insertion order of the first occurrence of each name is preserved so the
    display order stays stable.
    """
    # Maps name → (entry, version_tuple, is_latest)
    best: dict[str, tuple[dict, tuple[int, ...], bool]] = {}

    for entry in raw_entries:
        srv = entry.get("server", entry)
        name: str = srv.get("name", "")
        version_str: str = srv.get("version", "")
        version_tuple = _parse_semver(version_str)

        meta = entry.get("_meta", {})
        # registry key can vary; scan all top-level _meta values for isLatest
        is_latest = False
        for meta_val in meta.values():
            if isinstance(meta_val, dict) and meta_val.get("isLatest"):
                is_latest = True
                break

        if name not in best:
            best[name] = (entry, version_tuple, is_latest)
        else:
            _, cur_ver, cur_latest = best[name]
            # Prefer isLatest flag; among equal-flag entries prefer higher version.
            if is_latest and not cur_latest:
                best[name] = (entry, version_tuple, is_latest)
            elif not is_latest and cur_latest:
                pass  # current best wins
            elif version_tuple > cur_ver:
                best[name] = (entry, version_tuple, is_latest)

    return [item[0] for item in best.values()]


_DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"


def _base_urls() -> list[str]:
    """Resolve the ordered list of registry URLs to try.

    Mirrors :func:`reyn.api.safe.mcp.registry._registry_urls` so both
    surfaces — the async op-handler client (this module) and the
    safe-mode skill-internal lookup — agree on which registries to
    try and in what order.

    Priority:
    1. ``REYN_MCP_REGISTRY_URLS`` (plural) — comma-separated list,
       populated by the config loader from ``mcp.registries: [...]``
       when the env var isn't already explicitly set.
    2. ``REYN_MCP_REGISTRY_URL`` (singular, legacy) — single-item list.
    3. Default single-item list with the public registry.
    """
    plural = os.environ.get("REYN_MCP_REGISTRY_URLS")
    if plural:
        urls = [u.strip().rstrip("/") for u in plural.split(",")]
        return [u for u in urls if u]
    singular = os.environ.get("REYN_MCP_REGISTRY_URL")
    if singular:
        return [singular.strip().rstrip("/")]
    return [_DEFAULT_BASE_URL]


def _base_url() -> str:
    """Return the first registry URL (= preserved for backward compat)."""
    return _base_urls()[0]


class RegistryClient:
    """Async client for ``registry.modelcontextprotocol.io``.

    Usage::

        async with RegistryClient() as client:
            results = await client.search("slack")
            server  = await client.get_server("ai.smithery/smithery-ai-slack")

    SSL verification priority (matches ``web.fetch`` in ``reyn.yaml``):
      1. ``verify`` constructor arg set to a path string → use as CA bundle.
      2. ``verify`` set to ``False`` → disable SSL check.
      3. ``verify`` set to ``True``  → force SSL check.
      4. ``verify`` is ``None`` (default) → fall through to litellm.get_ssl_verify()
         (``SSL_VERIFY`` env → ``litellm.ssl_verify`` → ``SSL_CERT_FILE`` → ``True``).

    Callers that have a ``ReynConfig`` available should pass the resolved
    value from ``_resolve_ssl_verify_from_config(config.web.fetch)``
    (see ``reyn.core.op_runtime.web``).  The default ``None`` preserves the
    existing env-var behaviour so all current callers remain unaffected.
    """

    def __init__(self, verify: bool | str | None = None) -> None:
        self._client = None  # httpx.AsyncClient — lazy init
        self._verify = verify  # None = use litellm env-var fallback

    async def __aenter__(self) -> "RegistryClient":
        import httpx
        from litellm.llms.custom_httpx.http_handler import get_ssl_verify

        # Resolve the verify value: explicit arg takes priority; None falls
        # through to the litellm env-var chain (same as web_fetch handler).
        verify = self._verify if self._verify is not None else get_ssl_verify()
        # SSL verification — priority: constructor arg → litellm env-var chain.
        self._client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "reyn/1.0"},
            verify=verify,
            # #1956 SSRF: re-gate every hop (incl. redirects) via the IP-deny guard.
            event_hooks={"request": [_ssrf_request_hook]},
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        base_url: str | None = None,
    ) -> dict:
        """Issue a GET request and return the parsed JSON body.

        ``base_url`` (= optional, defaults to the first
        :func:`_base_urls` entry) lets callers iterate registries
        without bypassing this method — that keeps existing test
        mocks (= patches of ``RegistryClient._get``) intercepting
        every HTTP call regardless of whether single-URL or
        multi-URL semantics are in play.

        Raises ``RegistryError`` on network errors, non-2xx status
        codes, or JSON parse failures.
        """
        import httpx

        effective_base = base_url or _base_urls()[0]
        url = f"{effective_base}{path}"
        if self._client is None:
            raise RegistryError(
                "RegistryClient must be used as an async context manager."
            )
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise RegistryError(f"Registry request timed out: {url}") from exc
        except httpx.RequestError as exc:
            raise RegistryError(f"Registry unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise RegistryError(
                f"Registry returned HTTP {response.status_code} for {url}"
            )

        try:
            return response.json()
        except Exception as exc:
            raise RegistryError(f"Registry response is not valid JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: str, limit: int = 20) -> list:
        """Search for MCP servers matching *query*.

        Iterates the resolved registry URL list (:func:`_base_urls`)
        and returns the first non-empty result list — "private first,
        public fallback" semantics. A :class:`RegistryError` from one
        URL falls through to the next; if every URL fails the final
        error is re-raised.

        Returns a deduplicated list of ``ServerInfo`` dataclasses (from
        ``reyn.core.registry.models``).  When the registry returns multiple
        version entries for the same server name, only the latest
        version is kept: first by ``_meta.isLatest == true``, then by
        highest semver.

        Results are cached for 24 h.
        """
        from reyn.core.registry import cache
        from reyn.core.registry.models import server_info_from_raw

        cache_key = f"search:{query}:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            raw_entries = cached.get("servers", [])
            return [server_info_from_raw(e) for e in _dedup_by_latest(raw_entries)]

        last_error: RegistryError | None = None
        for base in _base_urls():
            try:
                data = await self._get(
                    "/v0.1/servers",
                    params={"search": query, "limit": str(limit)},
                    base_url=base,
                )
            except RegistryError as exc:
                last_error = exc
                continue
            raw_entries = data.get("servers", [])
            results = [server_info_from_raw(e) for e in _dedup_by_latest(raw_entries)]
            if results:
                cache.set(cache_key, data)
                return results
            # Empty result at this registry — fall through to next URL.
        if last_error is not None:
            raise last_error
        return []

    async def get_server(self, server_name: str) -> object:
        """Fetch the latest version of a specific server by registry name.

        Iterates the resolved registry URL list (:func:`_base_urls`).
        A 404 from one URL falls through to the next; the first
        non-404 hit returns. If every URL replies 404 or errors, the
        final error is re-raised (= preserves the pre-existing
        ``RegistryError`` contract for "server not found anywhere").

        *server_name* is the registry identifier (e.g.
        ``"io.github.foo/bar-mcp"``).

        Returns a ``ServerJson`` dataclass.  Result is cached for 24 h.
        """
        from reyn.core.registry import cache
        from reyn.core.registry.models import server_json_from_raw

        cache_key = f"server:{server_name}"
        cached = cache.get(cache_key)
        if cached is not None:
            srv = cached.get("server", cached)
            return server_json_from_raw(srv)

        last_error: RegistryError | None = None
        # #1447: registry IDs are reverse-DNS and always contain "/" (e.g.
        # io.github.foo/bar). The "/" MUST be percent-encoded or it injects extra
        # path segments → the registry 404s. (Mirrors safe/mcp/registry.py:246;
        # this parallel impl had the encoding gap → every registry install 404'd.)
        encoded_name = urllib.parse.quote(server_name, safe="")
        for base in _base_urls():
            try:
                data = await self._get(
                    f"/v0.1/servers/{encoded_name}/versions/latest",
                    base_url=base,
                )
            except RegistryError as exc:
                # 404 → not found on this URL, fall through to the next.
                # Other errors → remember as last_error in case every
                # URL fails.
                if "HTTP 404" not in str(exc):
                    last_error = exc
                else:
                    last_error = exc  # last 404 also surfaced if all fail
                continue
            cache.set(cache_key, data)
            srv = data.get("server", data)
            return server_json_from_raw(srv)

        # Every URL errored.
        assert last_error is not None
        raise last_error
