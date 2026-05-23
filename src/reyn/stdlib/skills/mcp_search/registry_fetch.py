"""Deterministic preprocessor for mcp_search — fetches MCP registry results.

Called as a ``type: python`` preprocessor step (mode: safe). Signature:
  ``fetch_registry_results(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``text``  — the user's natural language capability request (e.g. "Slack 連携").

Output (placed at ``data.registry``):
  ``candidates`` — list of {name, repo_url, description, runtime_hint} dicts.
  ``source``     — ``"registry"`` | ``"error"``
  ``query``      — keyword extracted and used for the registry search.

The keyword extraction is intentionally minimal and deterministic:
  Take the first English-looking word (ASCII letters only), fall back to the
  first whitespace-separated token of the input text.

Registry unreachable:
  On HTTP error or any other registry failure the preprocessor returns an
  empty candidates list with ``source="error"`` so the LLM phase can still
  finish gracefully. The 24-hour disk cache hidden inside
  ``reyn.safe.mcp.registry`` covers the "stale-but-available" case
  transparently — when the network is down but a recent search payload
  exists on disk, the safe-mode lookup returns the cached value and we
  surface it as ``source="registry"`` (= the legacy ``"registry_stale"``
  status is folded into the same path; the LLM contract no longer
  distinguishes fresh-vs-stale because the cache TTL is short).

FP-0042 Phase 2.4 (2026-05-23): migrated from mode: unsafe to mode: safe.
"""
from __future__ import annotations

import re

from reyn.safe.mcp.registry import RegistryError, search


def _extract_keyword(text: str) -> str:
    """Extract a concise English search keyword from the user's request.

    Strategy: find the first run of ASCII letters (≥ 3 chars) — these are
    almost always English product/service names embedded in Japanese or
    mixed-language text. If nothing qualifies, fall back to the first
    whitespace token lowercased (for purely English input).
    """
    match = re.search(r"[A-Za-z]{3,}", text)
    if match:
        return match.group(0).lower()
    token = text.split()[0] if text.split() else text
    return token.lower()


def fetch_registry_results(artifact: dict) -> dict:
    """Python preprocessor entry point.

    Receives the phase input artifact dict. Returns a dict placed at
    ``data.registry`` in the enriched artifact.
    """
    text: str = (artifact.get("data") or {}).get("text") or ""
    query = _extract_keyword(text) if text.strip() else ""
    if not query:
        return {"candidates": [], "source": "error", "query": ""}

    try:
        candidates = search(query, limit=20)
    except RegistryError:
        return {"candidates": [], "source": "error", "query": query}

    return {
        "candidates": candidates,
        "source": "registry",
        "query": query,
    }
