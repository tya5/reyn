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

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class RegistryError(Exception):
    """Raised when the registry is unreachable or returns an error."""


def _base_url() -> str:
    return os.environ.get(
        "REYN_MCP_REGISTRY_URL",
        "https://registry.modelcontextprotocol.io",
    ).rstrip("/")


class RegistryClient:
    """Async client for ``registry.modelcontextprotocol.io``.

    Usage::

        async with RegistryClient() as client:
            results = await client.search("slack")
            server  = await client.get_server("ai.smithery/smithery-ai-slack")
    """

    def __init__(self) -> None:
        self._client = None  # httpx.AsyncClient — lazy init

    async def __aenter__(self) -> "RegistryClient":
        import httpx

        self._client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "reyn/1.0"},
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """Issue a GET request and return the parsed JSON body.

        Raises ``RegistryError`` on network errors, non-2xx status codes,
        or JSON parse failures.
        """
        import httpx

        url = f"{_base_url()}{path}"
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

        Returns a list of ``ServerInfo`` dataclasses (from
        ``reyn.registry.models``).  Results are cached for 24 h.

        Raises ``RegistryError`` on network / HTTP failure.
        """
        from reyn.registry import cache
        from reyn.registry.models import server_info_from_raw

        cache_key = f"search:{query}:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            raw_entries = cached.get("servers", [])
            return [server_info_from_raw(e) for e in raw_entries]

        data = await self._get(
            "/v0.1/servers",
            params={"search": query, "limit": str(limit)},
        )
        cache.set(cache_key, data)
        raw_entries = data.get("servers", [])
        return [server_info_from_raw(e) for e in raw_entries]

    async def get_server(self, server_name: str) -> object:
        """Fetch the latest version of a specific server by registry name.

        *server_name* is the registry identifier (e.g.
        ``"io.github.foo/bar-mcp"``).

        Returns a ``ServerJson`` dataclass.  Result is cached for 24 h.

        Raises ``RegistryError`` on network / HTTP failure.
        """
        from reyn.registry import cache
        from reyn.registry.models import server_json_from_raw

        cache_key = f"server:{server_name}"
        cached = cache.get(cache_key)
        if cached is not None:
            srv = cached.get("server", cached)
            return server_json_from_raw(srv)

        data = await self._get(
            f"/v0.1/servers/{server_name}/versions/latest",
        )
        cache.set(cache_key, data)
        srv = data.get("server", data)
        return server_json_from_raw(srv)
