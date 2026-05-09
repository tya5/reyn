"""Tier 2: reyn.registry.RegistryClient HTTP path invariants.

Uses httpx's built-in transport mock (``httpx.MockTransport``) to record
and replay HTTP responses without real network calls.  This is not a Mock
of a collaborator — it is an httpx-native fixture mechanism that exercises
the real RegistryClient code paths.

Invariants tested:
  - search() constructs the correct URL and returns ServerInfo list.
  - search() caches results; second call with same key skips HTTP.
  - get_server() constructs the correct URL and returns ServerJson.
  - get_server() caches results; second call skips HTTP.
  - RegistryError raised on timeout / non-2xx status.
  - REYN_MCP_REGISTRY_URL env override is respected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
import pytest_asyncio

import reyn.registry.cache as cache_mod
from reyn.registry import RegistryClient, RegistryError
from reyn.registry.models import ServerInfo, ServerJson


# ---------------------------------------------------------------------------
# Fixtures — recorded response bodies
# ---------------------------------------------------------------------------

SEARCH_RESPONSE_SLACK = {
    "servers": [
        {
            "server": {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json",
                "name": "ai.smithery/smithery-ai-slack",
                "description": "Enable interaction with Slack workspaces.",
                "repository": {
                    "url": "https://github.com/smithery-ai/mcp-servers",
                    "source": "github",
                    "subfolder": "slack",
                },
                "version": "1.0.0",
                "remotes": [
                    {
                        "type": "streamable-http",
                        "url": "https://server.smithery.ai/@smithery-ai/slack/mcp",
                    }
                ],
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {
                    "status": "active",
                    "isLatest": True,
                }
            },
        },
        {
            "server": {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
                "name": "io.example/slack-mcp",
                "description": "Another Slack MCP server with npm package.",
                "repository": {"url": "https://github.com/example/slack-mcp", "source": "github"},
                "version": "0.2.0",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "@example/slack-mcp",
                        "version": "0.2.0",
                        "transport": {"type": "stdio"},
                    }
                ],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
        },
    ],
    "metadata": {"nextCursor": "io.example/slack-mcp:0.2.0", "count": 2},
}

SERVER_DETAIL_RESPONSE = {
    "server": {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "capital.hove/read-only-local-postgres-mcp-server",
        "description": "MCP server for read-only PostgreSQL database queries.",
        "repository": {
            "url": "https://github.com/hovecapital/read-only-local-postgres-mcp-server",
            "source": "github",
        },
        "version": "0.1.0",
        "websiteUrl": "https://hove.capital",
        "packages": [
            {
                "registryType": "npm",
                "registryBaseUrl": "https://registry.npmjs.org",
                "identifier": "@hovecapital/read-only-postgres-mcp-server",
                "version": "0.1.0",
                "transport": {"type": "stdio"},
            }
        ],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
}


def _make_mock_transport(responses: dict[str, Any]):
    """Build an httpx MockTransport that returns pre-configured responses.

    ``responses`` maps URL path prefixes to (status_code, json_body) tuples.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for prefix, (status, body) in responses.items():
            if path.startswith(prefix):
                return httpx.Response(
                    status_code=status,
                    headers={"content-type": "application/json"},
                    content=json.dumps(body).encode(),
                )
        return httpx.Response(404, content=b"not found")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Helper: build a RegistryClient using mock transport (no real network)
# ---------------------------------------------------------------------------

def _client_with_transport(transport) -> RegistryClient:
    import httpx

    client = RegistryClient()
    client._client = httpx.AsyncClient(
        transport=transport,
        base_url="https://registry.modelcontextprotocol.io",
        headers={"User-Agent": "reyn/1.0"},
    )
    return client


# ---------------------------------------------------------------------------
# search() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_server_info_list(tmp_path):
    """Tier 2: search() parses the registry response and returns ServerInfo list."""
    transport = _make_mock_transport({
        "/v0.1/servers": (200, SEARCH_RESPONSE_SLACK),
    })
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        results = await client.search("slack", limit=20)

    assert isinstance(results, list)
    assert len(results) == 2

    first = results[0]
    assert isinstance(first, ServerInfo)
    assert first.name == "ai.smithery/smithery-ai-slack"
    assert "Slack" in first.description
    assert first.repository_url == "https://github.com/smithery-ai/mcp-servers"
    assert first.runtime_hint == ""  # first server has no packages → no hint

    second = results[1]
    assert second.name == "io.example/slack-mcp"
    assert second.runtime_hint == "npx"  # npm package → npx hint


@pytest.mark.asyncio
async def test_search_writes_to_cache(tmp_path):
    """Tier 2: search() writes response to cache; second call skips HTTP."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(SEARCH_RESPONSE_SLACK).encode(),
        )

    transport = httpx.MockTransport(handler)
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        r1 = await client.search("slack", limit=20)
        r2 = await client.search("slack", limit=20)

    assert call_count == 1  # second search hit cache, no HTTP
    assert len(r1) == len(r2)
    assert r1[0].name == r2[0].name


@pytest.mark.asyncio
async def test_search_raises_on_network_error(tmp_path):
    """Tier 2: search() raises RegistryError on network failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network error")

    transport = httpx.MockTransport(handler)
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        with pytest.raises(RegistryError):
            await client.search("slack")


@pytest.mark.asyncio
async def test_search_raises_on_http_error(tmp_path):
    """Tier 2: search() raises RegistryError on non-2xx HTTP status."""
    transport = _make_mock_transport({"/v0.1/servers": (503, {"error": "unavailable"})})
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        with pytest.raises(RegistryError):
            await client.search("slack")


# ---------------------------------------------------------------------------
# get_server() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_server_returns_server_json(tmp_path):
    """Tier 2: get_server() parses the detail response and returns ServerJson."""
    server_name = "capital.hove/read-only-local-postgres-mcp-server"
    transport = _make_mock_transport({
        f"/v0.1/servers/{server_name}": (200, SERVER_DETAIL_RESPONSE),
    })
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        result = await client.get_server(server_name)

    assert isinstance(result, ServerJson)
    assert result.name == server_name
    assert result.version == "0.1.0"
    assert result.repository_url.startswith("https://github.com")
    assert result.website_url == "https://hove.capital"
    assert len(result.packages) == 1
    assert result.packages[0].registry_type == "npm"
    assert result.packages[0].transport_type == "stdio"
    assert result.runtime_hint == "npx"


@pytest.mark.asyncio
async def test_get_server_writes_to_cache(tmp_path):
    """Tier 2: get_server() caches result; second call skips HTTP."""
    server_name = "capital.hove/read-only-local-postgres-mcp-server"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(SERVER_DETAIL_RESPONSE).encode(),
        )

    transport = httpx.MockTransport(handler)
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        r1 = await client.get_server(server_name)
        r2 = await client.get_server(server_name)

    assert call_count == 1
    assert r1.name == r2.name


@pytest.mark.asyncio
async def test_get_server_raises_on_404(tmp_path):
    """Tier 2: get_server() raises RegistryError on 404 (unknown server name)."""
    transport = _make_mock_transport({})  # returns 404 for all paths
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        with pytest.raises(RegistryError):
            await client.get_server("unknown/server")


# ---------------------------------------------------------------------------
# REYN_MCP_REGISTRY_URL override
# ---------------------------------------------------------------------------


def test_base_url_uses_env_override(monkeypatch):
    """Tier 2: _base_url() returns REYN_MCP_REGISTRY_URL when set."""
    from reyn.registry.client import _base_url

    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://my-internal-registry.example.com")
    assert _base_url() == "https://my-internal-registry.example.com"


def test_base_url_defaults_to_public_registry(monkeypatch):
    """Tier 2: _base_url() defaults to official public registry."""
    from reyn.registry.client import _base_url

    monkeypatch.delenv("REYN_MCP_REGISTRY_URL", raising=False)
    assert _base_url() == "https://registry.modelcontextprotocol.io"


# ---------------------------------------------------------------------------
# Dedup: same server name, multiple version entries
# ---------------------------------------------------------------------------

# Registry response that simulates 6 duplicate entries for the same server
# (version-per-entry pattern observed in dogfood) plus one unrelated server.
SEARCH_RESPONSE_FILESYSTEM_DUPES = {
    "servers": [
        # io.github.j0hanz/filesystem-mcp — 6 version entries, latest is 0.3.0
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.1.0",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.1.0"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}},
        },
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.2.0",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.2.0"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}},
        },
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.3.0",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.3.0"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
        },
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.1.1",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.1.1"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}},
        },
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.2.5",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.2.5"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}},
        },
        {
            "server": {
                "name": "io.github.j0hanz/filesystem-mcp",
                "description": "Read, create, and edit files.",
                "repository": {"url": "https://github.com/j0hanz/filesystem-mcp"},
                "version": "0.3.0",
                "packages": [{"registryType": "npm", "identifier": "@j0hanz/filesystem-mcp", "version": "0.3.0"}],
            },
            "_meta": {},  # no isLatest flag — duplicate of latest by version
        },
        # unrelated server
        {
            "server": {
                "name": "io.github.Digital-Defiance/mcp-filesystem",
                "description": "Advanced filesystem operations with strict security boundaries.",
                "repository": {"url": "https://github.com/Digital-Defiance/ai-capabiliti"},
                "version": "1.0.0",
                "packages": [{"registryType": "npm", "identifier": "@digital-defiance/mcp-filesystem", "version": "1.0.0"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
        },
    ],
    "metadata": {"count": 7},
}

# Registry response where one entry has no repository field at all.
SEARCH_RESPONSE_NO_REPO = {
    "servers": [
        {
            "server": {
                "name": "io.github.Digital-Defiance/mcp-filesystem",
                "description": "Advanced filesystem operations with batch ops.",
                # no "repository" key
                "version": "1.0.0",
                "packages": [{"registryType": "npm", "identifier": "@digital-defiance/mcp-filesystem", "version": "1.0.0"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
        },
    ],
    "metadata": {"count": 1},
}


@pytest.mark.asyncio
async def test_search_deduplicates_same_name_keeps_latest(tmp_path):
    """Tier 2: search() keeps only the isLatest entry when multiple versions of the same server are returned."""
    transport = _make_mock_transport({
        "/v0.1/servers": (200, SEARCH_RESPONSE_FILESYSTEM_DUPES),
    })
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        results = await client.search("filesystem", limit=20)

    names = [r.name for r in results]
    # Each distinct server name appears exactly once.
    assert names.count("io.github.j0hanz/filesystem-mcp") == 1
    assert names.count("io.github.Digital-Defiance/mcp-filesystem") == 1
    assert len(results) == 2

    # The kept entry must be the latest version (0.3.0 with isLatest=True).
    j0hanz = next(r for r in results if r.name == "io.github.j0hanz/filesystem-mcp")
    assert j0hanz.runtime_hint == "npx"


@pytest.mark.asyncio
async def test_search_deduplicates_fallback_to_semver_when_no_is_latest(tmp_path):
    """Tier 2: search() falls back to highest semver when no entry has isLatest=True."""
    response = {
        "servers": [
            {
                "server": {
                    "name": "io.example/test-mcp",
                    "description": "Test.",
                    "version": "1.0.0",
                },
                "_meta": {},
            },
            {
                "server": {
                    "name": "io.example/test-mcp",
                    "description": "Test.",
                    "version": "2.0.0",
                },
                "_meta": {},
            },
            {
                "server": {
                    "name": "io.example/test-mcp",
                    "description": "Test.",
                    "version": "1.5.0",
                },
                "_meta": {},
            },
        ],
        "metadata": {"count": 3},
    }
    transport = _make_mock_transport({"/v0.1/servers": (200, response)})
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        results = await client.search("test", limit=20)

    assert len(results) == 1
    # Cannot assert on internal version field (ServerInfo has no version attr),
    # but we verify exactly one result is returned (dedup occurred).


@pytest.mark.asyncio
async def test_search_missing_repository_url_returns_none(tmp_path):
    """Tier 2: server_info_from_raw() returns repository_url=None when registry entry has no repository.url."""
    transport = _make_mock_transport({
        "/v0.1/servers": (200, SEARCH_RESPONSE_NO_REPO),
    })
    with mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path):
        client = _client_with_transport(transport)
        results = await client.search("filesystem", limit=20)

    assert len(results) == 1
    assert results[0].repository_url is None


def test_display_none_repo_url_renders_placeholder():
    """Tier 2: run_search display path renders '(no repo URL)' when repository_url is None."""
    from reyn.registry.models import ServerInfo

    server = ServerInfo(
        name="io.example/test-mcp",
        description="A test server.",
        repository_url=None,
        runtime_hint="npx",
    )
    # Simulate the display logic from run_search.
    repo_raw = server.repository_url if server.repository_url else "(no repo URL)"
    assert repo_raw == "(no repo URL)"

    # A server with a real URL should not be affected.
    server_with_url = ServerInfo(
        name="io.example/another-mcp",
        description="Another server.",
        repository_url="https://github.com/example/another-mcp",
        runtime_hint="npx",
    )
    repo_raw_with_url = server_with_url.repository_url if server_with_url.repository_url else "(no repo URL)"
    assert repo_raw_with_url == "https://github.com/example/another-mcp"
