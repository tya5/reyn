"""Tier 2: mcp_search registry_fetch preprocessor invariants.

Tests the deterministic preprocessor function ``fetch_registry_results``.

No LLM calls. Uses ``unittest.mock`` to patch the lowest-stable seam in
``reyn.safe.mcp.registry`` (= the internal ``_http_get_json`` helper) so
tests exercise the real keyword-extraction / cache / dedup / dict-shape
code path. The HTTP boundary is the only mocked thing; the rest is real.

Invariants:
  - Keyword extraction from mixed-language text is deterministic.
  - fetch_registry_results returns the expected dict shape.
  - On registry error, returns empty candidates with source="error".
  - Empty input text yields source="error" without HTTP calls.

FP-0042 Phase 2.4 (2026-05-23): tests updated to match the safe-mode
rewrite of ``mcp_search/registry_fetch.py``. The legacy
``"registry_stale"`` source was folded into ``"registry"`` because the
safe.mcp.registry layer handles cache transparency internally (= callers
don't distinguish fresh-vs-stale; both surface as ``"registry"``).
"""
from __future__ import annotations

from unittest import mock

import pytest

import reyn.registry.cache as cache_mod
import reyn.safe.mcp.registry as safe_registry
from reyn.stdlib.skills.mcp_search.registry_fetch import (
    _extract_keyword,
    fetch_registry_results,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SLACK_RESPONSE = {
    "servers": [
        {
            "server": {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json",
                "name": "ai.smithery/smithery-ai-slack",
                "description": "Enable interaction with Slack workspaces.",
                "repository": {
                    "url": "https://github.com/smithery-ai/mcp-servers",
                    "source": "github",
                },
                "version": "1.0.0",
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {
                    "status": "active",
                    "isLatest": True,
                }
            },
        }
    ],
    "metadata": {"count": 1},
}


# ---------------------------------------------------------------------------
# _extract_keyword — deterministic keyword extraction
# ---------------------------------------------------------------------------


def test_extract_keyword_english_word():
    """Tier 2: English-only input returns first word lowercased."""
    assert _extract_keyword("slack integration") == "slack"


def test_extract_keyword_japanese_with_english():
    """Tier 2: Mixed ja/en input extracts the English word."""
    assert _extract_keyword("Slack 連携できる MCP サーバーを探して") == "slack"


def test_extract_keyword_github_japanese():
    """Tier 2: 'GitHub リポジトリの操作' → 'github'."""
    assert _extract_keyword("GitHub リポジトリの操作") == "github"


def test_extract_keyword_short_word_skipped():
    """Tier 2: English tokens < 3 chars are skipped; first longer token is used."""
    # "DB 検索 PostgreSQL" — "DB" is 2 chars → skip; "PostgreSQL" qualifies.
    result = _extract_keyword("DB 検索 PostgreSQL")
    assert result == "postgresql"


def test_extract_keyword_empty_string():
    """Tier 2: Empty input returns empty string."""
    assert _extract_keyword("") == ""


def test_extract_keyword_purely_japanese():
    """Tier 2: Japanese-only input falls back to first token lowercased."""
    result = _extract_keyword("データベース連携")
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# fetch_registry_results — happy path
# ---------------------------------------------------------------------------


def _patch_safe_http(response_body: dict | None = None, raise_error: Exception | None = None):
    """Patch ``reyn.safe.mcp.registry._http_get_json`` to return a fixed payload
    or raise the given exception."""

    def _fake(url: str) -> dict:
        if raise_error is not None:
            raise raise_error
        return response_body or {}

    return mock.patch.object(safe_registry, "_http_get_json", _fake)


@pytest.fixture
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the disk cache to a per-test tmp dir so cache state doesn't leak."""
    monkeypatch.setattr(cache_mod, "_cache_dir", lambda: tmp_path)
    return tmp_path


def test_fetch_registry_happy_path(_isolated_cache):
    """Tier 2: fetch_registry_results returns expected dict shape on success."""
    artifact = {"data": {"text": "Slack 連携できる MCP サーバーを探して"}}

    with _patch_safe_http(_SLACK_RESPONSE):
        result = fetch_registry_results(artifact)

    assert result["source"] == "registry"
    assert result["query"] == "slack"

    cands = result["candidates"]
    assert isinstance(cands, list)
    assert len(cands) == 1
    c = cands[0]
    assert c["name"] == "ai.smithery/smithery-ai-slack"
    assert c["repo_url"] == "https://github.com/smithery-ai/mcp-servers"
    assert "Slack" in c["description"]


def test_fetch_registry_empty_candidates(_isolated_cache):
    """Tier 2: fetch_registry_results returns empty list when registry returns no servers."""
    artifact = {"data": {"text": "some very obscure thing"}}
    empty_response = {"servers": [], "metadata": {"count": 0}}

    with _patch_safe_http(empty_response):
        result = fetch_registry_results(artifact)

    assert result["candidates"] == []
    assert result["source"] == "registry"


# ---------------------------------------------------------------------------
# fetch_registry_results — error / fallback paths
# ---------------------------------------------------------------------------


def test_fetch_registry_error_returns_empty(_isolated_cache):
    """Tier 2: registry error with no cache returns source='error' + empty list."""
    artifact = {"data": {"text": "PostgreSQL database"}}

    with _patch_safe_http(raise_error=safe_registry.RegistryError("HTTP 503")):
        result = fetch_registry_results(artifact)

    assert result["candidates"] == []
    assert result["source"] == "error"


def test_fetch_registry_cached_response_used_when_http_errors(_isolated_cache):
    """Tier 2: when a previous successful response is cached, a subsequent
    HTTP error still surfaces the cached candidates (= cache hit short-circuits
    the HTTP path entirely, no error visible to caller)."""
    # Prime the cache with a real response.
    artifact = {"data": {"text": "Slack integration"}}
    with _patch_safe_http(_SLACK_RESPONSE):
        first = fetch_registry_results(artifact)
    assert first["source"] == "registry"
    assert len(first["candidates"]) == 1

    # Now make HTTP error — should still see cached candidates because the
    # cache is hot for the same query key.
    with _patch_safe_http(raise_error=safe_registry.RegistryError("HTTP 503")):
        second = fetch_registry_results(artifact)

    assert second["source"] == "registry"
    assert len(second["candidates"]) == 1


def test_fetch_registry_empty_text(_isolated_cache):
    """Tier 2: empty input returns source='error' without invoking HTTP."""
    artifact = {"data": {"text": ""}}
    call_count = 0

    def _spy(url: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {"servers": []}

    with mock.patch.object(safe_registry, "_http_get_json", _spy):
        result = fetch_registry_results(artifact)

    assert result["candidates"] == []
    assert result["source"] == "error"
    assert call_count == 0


def test_fetch_registry_missing_data_key(_isolated_cache):
    """Tier 2: artifact without 'data' key is handled gracefully."""
    artifact = {}

    with _patch_safe_http(raise_error=safe_registry.RegistryError("HTTP 503")):
        result = fetch_registry_results(artifact)

    assert result["candidates"] == []
    assert result["source"] == "error"
