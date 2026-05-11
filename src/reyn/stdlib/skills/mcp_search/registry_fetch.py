"""Deterministic preprocessor for mcp_search — fetches MCP registry results.

Called as a ``type: python`` preprocessor step.  Signature contract:
  ``fetch_registry_results(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``text``  — the user's natural language capability request (e.g. "Slack 連携").

Output (placed at ``data.registry``):
  ``candidates`` — list of {name, repo_url, description} dicts from the registry.
  ``source``     — ``"registry"`` | ``"registry_stale"`` | ``"error"``
  ``query``      — keyword extracted and used for the registry search.

The keyword extraction is intentionally minimal and deterministic:
  Take the first English-looking word (ASCII letters only), fall back to the
  first whitespace-separated token of the input text.

Registry unreachable:
  On HTTP error or any exception the preprocessor returns an empty candidates
  list with ``source="error"`` so the LLM phase can still finish with an empty
  result rather than crashing the phase.

I/O route: ``reyn.api.unsafe.http.get`` (= urllib, no extra deps).
JSON parse: ``reyn.api.safe.json.loads_strict``.
Caching: ``reyn.registry.cache`` (file-based TTL cache, 24 h).
"""
from __future__ import annotations

import os
import re

from reyn.api.safe.json import loads_strict
from reyn.api.unsafe.http import get as http_get


def _base_url() -> str:
    return os.environ.get(
        "REYN_MCP_REGISTRY_URL",
        "https://registry.modelcontextprotocol.io",
    ).rstrip("/")


def _extract_keyword(text: str) -> str:
    """Extract a concise English search keyword from the user's request.

    Strategy: find the first run of ASCII letters (≥ 3 chars) — these are
    almost always English product/service names embedded in Japanese or
    mixed-language text.  If nothing qualifies, fall back to the first
    whitespace token lowercased (for purely English input).
    """
    # Try to find an English word (≥ 3 ASCII letters) — handles mixed ja/en text.
    match = re.search(r"[A-Za-z]{3,}", text)
    if match:
        return match.group(0).lower()
    # Fallback: first whitespace-separated token.
    token = text.split()[0] if text.split() else text
    return token.lower()


def _search_registry(query: str, limit: int = 20) -> dict:
    """Fetch search results from the registry HTTP API.

    Returns the parsed JSON response dict.
    Raises ``RuntimeError`` on non-2xx status or JSON parse failure.
    """
    url = f"{_base_url()}/v0.1/servers"
    # Build query string manually — urllib does not support params kwarg.
    import urllib.parse
    qs = urllib.parse.urlencode({"search": query, "limit": str(limit)})
    resp = http_get(f"{url}?{qs}", headers={"User-Agent": "reyn/1.0"})
    if resp["status"] >= 400:
        raise RuntimeError(f"Registry returned HTTP {resp['status']}")
    return loads_strict(resp["body"])


def _dedup_and_extract(raw_entries: list[dict]) -> list[dict]:
    """Deduplicate and convert raw registry entries to candidate dicts."""
    from reyn.registry.client import _dedup_by_latest
    from reyn.registry.models import server_info_from_raw

    deduped = _dedup_by_latest(raw_entries)
    candidates = []
    for entry in deduped:
        info = server_info_from_raw(entry)
        if info.name:
            candidates.append(
                {
                    "name": info.name,
                    "repo_url": info.repository_url,
                    "description": info.description,
                }
            )
    return candidates


def fetch_registry_results(artifact: dict) -> dict:
    """Python preprocessor entry point.

    Receives the phase input artifact dict.  Returns a dict placed at
    ``data.registry`` in the enriched artifact.
    """
    import reyn.registry.cache as cache

    text: str = (artifact.get("data") or {}).get("text") or ""
    query = _extract_keyword(text) if text.strip() else ""
    if not query:
        return {"candidates": [], "source": "error", "query": ""}

    limit = 20
    cache_key = f"search:{query}:{limit}"

    # Cache hit — serve without network.
    cached = cache.get(cache_key)
    if cached is not None:
        raw_entries = cached.get("servers", [])
        return {
            "candidates": _dedup_and_extract(raw_entries),
            "source": "registry",
            "query": query,
        }

    try:
        data = _search_registry(query, limit=limit)
        cache.set(cache_key, data)
        raw_entries = data.get("servers", [])
        return {
            "candidates": _dedup_and_extract(raw_entries),
            "source": "registry",
            "query": query,
        }
    except Exception:
        # Registry unreachable — try stale cache before giving up.
        stale = cache.get(cache_key)
        if stale:
            raw_entries = stale.get("servers", [])
            return {
                "candidates": _dedup_and_extract(raw_entries),
                "source": "registry_stale",
                "query": query,
            }
        return {"candidates": [], "source": "error", "query": query}
