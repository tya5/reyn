"""Tier 2 — reyn.safe.mcp.registry contract tests (FP-0042 Phase 2.4).

Tests the ``search`` and ``lookup`` public surface that safe-mode skills
import. The HTTP boundary (``_http_get_json``) is the only mocked seam;
everything else (= cache, dedup, dict reshape, 404 → None handling) runs
real against a per-test tmp cache dir.

Threat-model rationale (= URL hardcoded, no permission gate, ambient
treatment) is documented in the module docstring and Issue #571.
"""
from __future__ import annotations

from unittest import mock

import pytest

import reyn.registry.cache as cache_mod
import reyn.safe.mcp.registry as sr

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the disk cache to per-test tmp dir."""
    monkeypatch.setattr(cache_mod, "_cache_dir", lambda: tmp_path)
    return tmp_path


_SEARCH_RESPONSE = {
    "servers": [
        {
            "server": {
                "name": "io.github.foo/bar-mcp",
                "description": "Bar MCP server",
                "repository": {"url": "https://github.com/foo/bar-mcp"},
                "version": "1.0.0",
                "packages": [{"registryType": "npm", "identifier": "@foo/bar"}],
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {"isLatest": True},
            },
        }
    ],
    "metadata": {"count": 1},
}

_VERSIONS_LATEST_RESPONSE = {
    "server": {
        "name": "io.github.modelcontextprotocol/server-filesystem",
        "description": "Filesystem MCP server",
        "version": "0.6.2",
        "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
        "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
        "packages": [{"registryType": "npm", "identifier": "@modelcontextprotocol/server-filesystem"}],
        "remotes": [],
    }
}


def _patch_http(payload=None, raise_error=None):
    """Patch ``_http_get_json`` to return a payload or raise an error."""

    def _fake(url: str) -> dict:
        if raise_error is not None:
            raise raise_error
        return payload or {}

    return mock.patch.object(sr, "_http_get_json", _fake)


# ── search ─────────────────────────────────────────────────────────────────


def test_search_returns_dict_list_with_expected_shape(_isolated_cache):
    """Tier 2: search returns list of dicts shaped {name, description, repo_url, runtime_hint}."""
    with _patch_http(_SEARCH_RESPONSE):
        result = sr.search("bar")

    assert isinstance(result, list)
    assert len(result) == 1
    entry = result[0]
    assert set(entry.keys()) == {"name", "description", "repo_url", "runtime_hint"}
    assert entry["name"] == "io.github.foo/bar-mcp"
    assert entry["repo_url"] == "https://github.com/foo/bar-mcp"
    assert entry["runtime_hint"] == "npx"


def test_search_empty_query_returns_empty_list_no_http(_isolated_cache):
    """Tier 2: empty query short-circuits, no HTTP call."""
    call_count = 0

    def _spy(url: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {}

    with mock.patch.object(sr, "_http_get_json", _spy):
        assert sr.search("") == []

    assert call_count == 0


def test_search_caches_response_disk(_isolated_cache):
    """Tier 2: second call with same query reads from cache, no HTTP call."""
    call_count = 0

    def _counting_http(url: str) -> dict:
        nonlocal call_count
        call_count += 1
        return _SEARCH_RESPONSE

    with mock.patch.object(sr, "_http_get_json", _counting_http):
        first = sr.search("bar", limit=10)
        second = sr.search("bar", limit=10)

    assert first == second
    assert call_count == 1, f"Expected 1 HTTP call (cache hit on 2nd), got {call_count}"


def test_search_propagates_registry_error(_isolated_cache):
    """Tier 2: RegistryError from the HTTP layer propagates to the caller."""
    with _patch_http(raise_error=sr.RegistryError("HTTP 503")):
        with pytest.raises(sr.RegistryError, match="HTTP 503"):
            sr.search("anything")


def test_search_dedups_multiple_versions(_isolated_cache):
    """Tier 2: when registry returns multiple versions of one server, only
    the latest survives (= reuses reyn.registry.client._dedup_by_latest)."""
    payload = {
        "servers": [
            {
                "server": {
                    "name": "io.github.foo/dup",
                    "description": "v1",
                    "version": "1.0.0",
                    "repository": {"url": "https://example.com/dup"},
                    "packages": [{"registryType": "npm", "identifier": "@foo/dup"}],
                },
            },
            {
                "server": {
                    "name": "io.github.foo/dup",
                    "description": "v2",
                    "version": "2.0.0",
                    "repository": {"url": "https://example.com/dup"},
                    "packages": [{"registryType": "npm", "identifier": "@foo/dup"}],
                },
                "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
            },
        ]
    }
    with _patch_http(payload):
        result = sr.search("dup")

    assert len(result) == 1
    assert result[0]["description"] == "v2"  # latest wins


# ── lookup ─────────────────────────────────────────────────────────────────


def test_lookup_returns_dict_on_hit(_isolated_cache):
    """Tier 2: lookup of a known server_id returns the dict shape."""
    with _patch_http(_VERSIONS_LATEST_RESPONSE):
        result = sr.lookup("io.github.modelcontextprotocol/server-filesystem")

    assert result is not None
    assert result["name"] == "io.github.modelcontextprotocol/server-filesystem"
    assert result["repo_url"] == "https://github.com/modelcontextprotocol/servers"


def test_lookup_returns_none_on_404(_isolated_cache):
    """Tier 2: a 404 from the registry surfaces as None (not raise)."""
    with _patch_http(raise_error=sr.RegistryError("HTTP 404 for ...")):
        result = sr.lookup("does/not-exist")

    assert result is None


def test_lookup_propagates_non_404_errors(_isolated_cache):
    """Tier 2: registry errors other than 404 propagate."""
    with _patch_http(raise_error=sr.RegistryError("HTTP 503 transient")):
        with pytest.raises(sr.RegistryError, match="503"):
            sr.lookup("any/thing")


def test_lookup_empty_id_returns_none(_isolated_cache):
    """Tier 2: empty server_id short-circuits to None, no HTTP call."""
    call_count = 0

    def _spy(url: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {}

    with mock.patch.object(sr, "_http_get_json", _spy):
        assert sr.lookup("") is None

    assert call_count == 0


def test_lookup_caches_response(_isolated_cache):
    """Tier 2: second lookup of same server_id reads from cache."""
    call_count = 0

    def _counting_http(url: str) -> dict:
        nonlocal call_count
        call_count += 1
        return _VERSIONS_LATEST_RESPONSE

    with mock.patch.object(sr, "_http_get_json", _counting_http):
        first = sr.lookup("io.github.modelcontextprotocol/server-filesystem")
        second = sr.lookup("io.github.modelcontextprotocol/server-filesystem")

    assert first == second
    assert call_count == 1


# ── URL hardcoding regression ─────────────────────────────────────────────


def test_base_url_is_hardcoded():
    """Tier 2: regression guard — the registry URL must stay hardcoded to the
    official MCP registry. If a future change adds env-var or config-driven
    override, Issue #571 needs revisiting first (= permission shape decision)."""
    assert sr._BASE_URL == "https://registry.modelcontextprotocol.io"
