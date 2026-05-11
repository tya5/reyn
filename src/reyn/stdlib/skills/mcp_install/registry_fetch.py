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

I/O route: ``reyn.api.unsafe.http.get`` (= urllib, no extra deps).
JSON parse: ``reyn.api.safe.json.loads_strict``.
Caching: ``reyn.registry.cache`` (file-based TTL cache, 24 h).
"""
from __future__ import annotations

import os
import re
import urllib.parse

from reyn.api.safe.json import loads_strict
from reyn.api.unsafe.http import get as http_get


def _base_url() -> str:
    return os.environ.get(
        "REYN_MCP_REGISTRY_URL",
        "https://registry.modelcontextprotocol.io",
    ).rstrip("/")


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


def _http_get_json(url: str) -> dict:
    """GET *url*, parse body as JSON. Raises RuntimeError on non-2xx or parse error."""
    resp = http_get(url, headers={"User-Agent": "reyn/1.0"})
    if resp["status"] >= 400:
        raise RuntimeError(f"Registry returned HTTP {resp['status']} for {url}")
    return loads_strict(resp["body"])


def _direct_lookup(server_id: str) -> dict:
    """Attempt to look up a specific server_id; return registry result dict."""
    import reyn.registry.cache as cache

    cache_key = f"server:{server_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return {
            "server_id": server_id,
            "candidates": [],
            "source": "direct",
            "query": server_id,
        }

    url = f"{_base_url()}/v0.1/servers/{urllib.parse.quote(server_id, safe='')}/versions/latest"
    try:
        data = _http_get_json(url)
        cache.set(cache_key, data)
        return {
            "server_id": server_id,
            "candidates": [],
            "source": "direct",
            "query": server_id,
        }
    except RuntimeError:
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


def _search_lookup(query: str) -> dict:
    """Search the registry and return candidates."""
    import reyn.registry.cache as cache
    from reyn.registry.client import _dedup_by_latest
    from reyn.registry.models import server_info_from_raw

    limit = 10
    cache_key = f"search:{query}:{limit}"
    cached = cache.get(cache_key)

    if cached is not None:
        data = cached
    else:
        qs = urllib.parse.urlencode({"search": query, "limit": str(limit)})
        url = f"{_base_url()}/v0.1/servers?{qs}"
        try:
            data = _http_get_json(url)
            cache.set(cache_key, data)
        except Exception:
            return {
                "server_id": "",
                "candidates": [],
                "source": "error",
                "query": query,
            }

    raw_entries = data.get("servers", [])
    deduped = _dedup_by_latest(raw_entries)
    candidates = []
    for entry in deduped:
        info = server_info_from_raw(entry)
        if info.name:
            candidates.append(
                {
                    "name": info.name,
                    "description": info.description,
                    "repo_url": info.repository_url,
                }
            )

    if not candidates:
        return {
            "server_id": "",
            "candidates": [],
            "source": "not_found",
            "query": query,
        }

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
        return _direct_lookup(text)

    query = _extract_keyword(text)
    if not query:
        return {
            "server_id": "",
            "candidates": [],
            "source": "error",
            "query": "",
        }

    return _search_lookup(query)
