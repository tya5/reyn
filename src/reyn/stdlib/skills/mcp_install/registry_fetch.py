"""Deterministic preprocessor for mcp_install — resolves user request to server_id.

Called as a ``type: python`` preprocessor step. Signature contract:
  ``fetch_server_for_install(artifact: dict) -> dict``

Input (from ``artifact["data"]``):
  ``text`` — user's natural language install request or explicit server_id.

Output (placed at ``data.registry``):
  ``server_id``  — exact registry identifier if resolved; "" if ambiguous or not found.
  ``candidates`` — list of {name, description, repo_url} when search returns results.
  ``source``     — "direct" | "search" | "not_found" | "error"
  ``query``      — the search query used (or the original text if direct lookup).

Resolution strategy:
  1. If text contains "/" — treat as explicit server_id; skip search.
  2. Otherwise — search the registry with the text as query.
     If exactly one result: use it as server_id (source="direct").
     If multiple results: return candidates (source="search").
     If zero results: source="not_found".
"""
from __future__ import annotations

import asyncio
import re


def _looks_like_server_id(text: str) -> bool:
    """Return True if text appears to be an explicit registry server_id.

    Heuristic: contains '/' and looks like 'namespace/server-name'
    (e.g. 'io.github.foo/bar-mcp').
    """
    stripped = text.strip()
    return "/" in stripped and not stripped.startswith("http")


def _extract_keyword(text: str) -> str:
    """Extract a concise search keyword from the install request."""
    match = re.search(r"[A-Za-z]{3,}", text)
    if match:
        return match.group(0).lower()
    token = text.split()[0] if text.split() else text
    return token.lower()


async def _direct_lookup(server_id: str) -> dict:
    """Attempt to look up a specific server_id; return registry dict."""
    from reyn.registry.client import RegistryClient, RegistryError

    try:
        async with RegistryClient() as client:
            server = await client.get_server(server_id)
        return {
            "server_id": server_id,
            "candidates": [],
            "source": "direct",
            "query": server_id,
        }
    except RegistryError:
        return {
            "server_id": "",
            "candidates": [],
            "source": "not_found",
            "query": server_id,
        }
    except Exception:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": server_id,
        }


async def _search_lookup(query: str) -> dict:
    """Search the registry and return candidates."""
    from reyn.registry.client import RegistryClient, RegistryError

    try:
        async with RegistryClient() as client:
            results = await client.search(query, limit=10)
        if not results:
            return {
                "server_id": "",
                "candidates": [],
                "source": "not_found",
                "query": query,
            }
        candidates = [
            {
                "name": r.name,
                "description": r.description,
                "repo_url": r.repository_url,
            }
            for r in results
            if r.name
        ]
        # If exactly one result, treat as direct match.
        if len(candidates) == 1:
            return {
                "server_id": candidates[0]["name"],
                "candidates": candidates,
                "source": "direct",
                "query": query,
            }
        return {
            "server_id": "",
            "candidates": candidates,
            "source": "search",
            "query": query,
        }
    except RegistryError:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": query,
        }
    except Exception:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": query,
        }


def fetch_server_for_install(artifact: dict) -> dict:
    """Python preprocessor entry point.

    Receives the phase input artifact dict. Returns a dict placed at
    ``data.registry`` in the enriched artifact.
    """
    text: str = (artifact.get("data") or {}).get("text") or ""
    text = text.strip()

    if not text:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": "",
        }

    if _looks_like_server_id(text):
        return asyncio.run(_direct_lookup(text))

    query = _extract_keyword(text)
    if not query:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": "",
        }

    return asyncio.run(_search_lookup(query))
