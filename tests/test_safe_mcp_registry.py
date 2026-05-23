"""Tier 2 — reyn.safe.mcp.registry contract tests (FP-0042 Phase 2.4).

Tests the ``search`` and ``lookup`` public surface that safe-mode skills
import. The HTTP boundary (``_http_get_json``) is the only mocked seam;
everything else (= cache, dedup, dict reshape, 404 → None handling) runs
real against a per-test tmp cache dir.

URL resolution: PR-9 (post #571 collapse arc, 2026-05-24) made the
base URL resolution honor ``REYN_MCP_REGISTRY_URL`` env var, mirroring
the chain used by ``reyn.registry.client._base_url``. See the "URL
resolution" section below for the tests pinning that behavior.

Threat-model rationale (= ambient registry lookup, no per-skill
``http.get`` declaration required; operator-trusted via the env var)
is documented in the module docstring.
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
    (entry,) = result
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

    (only,) = result
    assert only["description"] == "v2"  # latest wins


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


# ── URL resolution ────────────────────────────────────────────────────────


def test_base_url_default_is_official_registry(monkeypatch):
    """Tier 2: default base URL falls back to the official MCP registry."""
    monkeypatch.delenv("REYN_MCP_REGISTRY_URL", raising=False)
    assert sr._base_url() == "https://registry.modelcontextprotocol.io"


def test_base_url_honors_env_var(monkeypatch):
    """Tier 2: ``REYN_MCP_REGISTRY_URL`` overrides the default URL.

    Mirrors the resolution chain in
    ``reyn.registry.client._base_url`` so an operator-set private /
    corporate registry applies uniformly to both the async op-handler
    client and this safe-mode skill-internal lookup.
    """
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://private.example.com/mcp")
    assert sr._base_url() == "https://private.example.com/mcp"


def test_base_url_strips_trailing_slash(monkeypatch):
    """Tier 2: trailing slash on the env-var value is normalised away."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://private.example.com/mcp/")
    assert sr._base_url() == "https://private.example.com/mcp"


def test_search_uses_overridden_base_url(monkeypatch, _isolated_cache):
    """Tier 2: search hits the overridden host when ``REYN_MCP_REGISTRY_URL`` is set."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://private.example.com/mcp")
    captured_urls: list[str] = []

    def _fake_get(url):
        captured_urls.append(url)
        return _SEARCH_RESPONSE

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        sr.search("bar")

    (url,) = captured_urls
    assert url.startswith("https://private.example.com/mcp/v0.1/servers?")


def test_lookup_uses_overridden_base_url(monkeypatch, _isolated_cache):
    """Tier 2: lookup hits the overridden host when ``REYN_MCP_REGISTRY_URL`` is set."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://private.example.com/mcp")
    captured_urls: list[str] = []

    def _fake_get(url):
        captured_urls.append(url)
        return _VERSIONS_LATEST_RESPONSE

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        sr.lookup("io.github.modelcontextprotocol/server-filesystem")

    (url,) = captured_urls
    assert url.startswith("https://private.example.com/mcp/v0.1/servers/")


# ── multi-registry list (PR-10) ──────────────────────────────────────────


def test_registry_urls_plural_env_var_takes_priority(monkeypatch):
    """Tier 2: ``REYN_MCP_REGISTRY_URLS`` (plural) wins over singular."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://private.example.com,https://public.example.com")
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://singular.example.com")
    assert sr._registry_urls() == [
        "https://private.example.com",
        "https://public.example.com",
    ]


def test_registry_urls_falls_back_to_singular(monkeypatch):
    """Tier 2: when plural is unset, singular env var becomes a one-item list."""
    monkeypatch.delenv("REYN_MCP_REGISTRY_URLS", raising=False)
    monkeypatch.setenv("REYN_MCP_REGISTRY_URL", "https://singular.example.com")
    assert sr._registry_urls() == ["https://singular.example.com"]


def test_registry_urls_default(monkeypatch):
    """Tier 2: with neither env var set, default to the public registry."""
    monkeypatch.delenv("REYN_MCP_REGISTRY_URLS", raising=False)
    monkeypatch.delenv("REYN_MCP_REGISTRY_URL", raising=False)
    assert sr._registry_urls() == ["https://registry.modelcontextprotocol.io"]


def test_registry_urls_strips_trailing_slashes(monkeypatch):
    """Tier 2: trailing slashes are normalised per-entry."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://a.example/, https://b.example/mcp/")
    assert sr._registry_urls() == ["https://a.example", "https://b.example/mcp"]


def test_lookup_iterates_on_404_fallback(monkeypatch, _isolated_cache):
    """Tier 2: lookup falls through to the next URL on 404 from the first."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://private.example.com,https://public.example.com")
    captured_urls: list[str] = []

    def _fake_get(url):
        captured_urls.append(url)
        if "private.example.com" in url:
            raise sr.RegistryError(f"Registry returned HTTP 404 for {url}")
        return _VERSIONS_LATEST_RESPONSE

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        result = sr.lookup("io.github.modelcontextprotocol/server-filesystem")

    # Both URLs tried; second returns the hit.
    (first_url, second_url) = captured_urls
    assert "private.example.com" in first_url
    assert "public.example.com" in second_url
    assert result is not None


def test_lookup_returns_none_when_all_404(monkeypatch, _isolated_cache):
    """Tier 2: lookup returns None when every URL replies 404 (= "not found anywhere").

    Preserves the pre-existing single-URL semantics (= 404 → None) across
    the multi-URL fallback. Non-404 errors still bubble up.
    """
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://a.example,https://b.example")

    def _fake_get(url):
        raise sr.RegistryError(f"Registry returned HTTP 404 for {url}")

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        result = sr.lookup("io.github.modelcontextprotocol/server-filesystem")
    assert result is None


def test_lookup_raises_when_non_404_after_404(monkeypatch, _isolated_cache):
    """Tier 2: lookup re-raises non-404 when later URL fails after earlier 404."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://a.example,https://b.example")

    def _fake_get(url):
        if "a.example" in url:
            raise sr.RegistryError(f"Registry returned HTTP 404 for {url}")
        raise sr.RegistryError(f"Registry returned HTTP 500 for {url}")

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        with pytest.raises(sr.RegistryError, match="500"):
            sr.lookup("io.github.modelcontextprotocol/server-filesystem")


def test_search_iterates_on_empty_first_returns_second(monkeypatch, _isolated_cache):
    """Tier 2: search falls through to the next URL when the first returns empty."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://private.example.com,https://public.example.com")
    captured_urls: list[str] = []

    def _fake_get(url):
        captured_urls.append(url)
        if "private.example.com" in url:
            return {"servers": [], "metadata": {"count": 0}}
        return _SEARCH_RESPONSE

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        result = sr.search("bar")

    (first_url, second_url) = captured_urls
    assert "private.example.com" in first_url
    assert "public.example.com" in second_url
    assert result and result[0]["name"] == "io.github.foo/bar-mcp"


def test_search_first_hit_wins(monkeypatch, _isolated_cache):
    """Tier 2: search returns the first non-empty result without trying later URLs."""
    monkeypatch.setenv("REYN_MCP_REGISTRY_URLS", "https://private.example.com,https://public.example.com")
    captured_urls: list[str] = []

    def _fake_get(url):
        captured_urls.append(url)
        return _SEARCH_RESPONSE  # private has the hit

    with mock.patch.object(sr, "_http_get_json", side_effect=_fake_get):
        sr.search("bar")

    # Only private was hit (= "private first" semantics).
    (only_url,) = captured_urls
    assert "private.example.com" in only_url
