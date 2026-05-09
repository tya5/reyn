"""Deterministic preprocessor for mcp_search — fetches MCP registry results.

Called as a ``type: python`` preprocessor step.  Signature contract:
  ``fetch_registry_results(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``text``  — the user's natural language capability request (e.g. "Slack 連携").

Output (placed at ``data.registry``):
  ``candidates`` — list of {name, repo_url, description} dicts from the registry.
  ``source``     — ``"registry"`` | ``"fallback"``
  ``query``      — keyword extracted and used for the registry search.

The keyword extraction is intentionally minimal and deterministic:
  Take the first English-looking word (ASCII letters only), fall back to the
  first whitespace-separated token of the input text.

Registry unreachable:
  On ``RegistryError`` or any exception the preprocessor returns an empty
  candidates list with ``source="error"`` so the LLM phase can still finish
  with an empty result rather than crashing the phase.
"""
from __future__ import annotations

import asyncio
import re


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


async def _do_fetch(query: str) -> dict:
    from reyn.registry import RegistryClient, RegistryError, cache

    limit = 20
    cache_key = f"search:{query}:{limit}"

    try:
        async with RegistryClient() as client:
            results = await client.search(query, limit=limit)
        candidates = [
            {
                "name": r.name,
                "repo_url": r.repository_url,
                "description": r.description,
            }
            for r in results
            if r.name  # skip empty names
        ]
        return {"candidates": candidates, "source": "registry", "query": query}
    except RegistryError:
        # Registry unreachable — try stale cache before giving up.
        stale = cache.get(cache_key)
        if stale:
            raw_entries = stale.get("servers", [])
            from reyn.registry.models import server_info_from_raw
            results_stale = [server_info_from_raw(e) for e in raw_entries]
            candidates = [
                {
                    "name": r.name,
                    "repo_url": r.repository_url,
                    "description": r.description,
                }
                for r in results_stale
                if r.name
            ]
            return {"candidates": candidates, "source": "registry_stale", "query": query}
        return {"candidates": [], "source": "error", "query": query}
    except Exception:
        return {"candidates": [], "source": "error", "query": query}


def fetch_registry_results(artifact: dict) -> dict:
    """Python preprocessor entry point.

    Receives the phase input artifact dict.  Returns a dict placed at
    ``data.registry`` in the enriched artifact.
    """
    text: str = (artifact.get("data") or {}).get("text") or ""
    query = _extract_keyword(text) if text.strip() else ""
    if not query:
        return {"candidates": [], "source": "error", "query": ""}

    # Run the async fetch in a new event loop (safe inside subprocess harness).
    return asyncio.run(_do_fetch(query))
