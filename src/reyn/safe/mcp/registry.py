"""Safe-mode MCP server registry lookup (FP-0042 Phase 2.4).

Exposes ``search(query, limit)`` and ``lookup(server_id)`` to safe-mode
python steps. Internally batches HTTP GET against
``registry.modelcontextprotocol.io``, caches responses on disk (~/.reyn/
registry-cache/), parses + dedups the registry envelope, and returns
plain JSON-serialisable dicts.

Threat model + permission rationale
-----------------------------------

The registry URL is **hardcoded** to the official MCP server registry.
There is no env var override, no config-driven base_url, and no
per-skill host allowlist. As a result, registry lookup is treated as an
*ambient* operation in the same sense as ``time`` and ``random`` —
operator-controlled state is not in the loop, so the operation has no
permission gate.

If a future requirement needs operator-configurable registry URLs (=
private mirror / corporate registry), the design question moves into
Issue #571 ("Permission model: granularity decomposition vs abstraction
granularity"); at that point the URL becomes config-controlled and the
permission shape needs to be co-designed with the rest of the MCP
permission family.

Internal layering
-----------------

This module is reyn-package internal code (= not subject to the
safe-mode AST validator). It is free to import urllib / reyn.registry
helpers; the validator only rejects user-code imports outside the
allowlist, and ``reyn.safe.*`` is admitted.

Public API (returned dict shape)
--------------------------------

Both ``search`` and ``lookup`` return plain dicts (= JSON-friendly, no
dataclasses) so safe-mode skills can pass results through artifact
boundaries without translation:

::

    {
        "name":         "io.github.foo/bar-mcp",
        "description":  "One-line description from server.json",
        "repo_url":     "https://github.com/foo/bar-mcp" | None,
        "runtime_hint": "npx" | "uvx" | "docker" | "dnx" | "",
    }
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Internal reuse of the non-safe-namespaced helpers. These are pure
# (= dedup is a list transform; server_info_from_raw is a dict reshape)
# or scoped to reyn-internal disk cache. None of them are reachable from
# user code through this safe namespace surface.
from reyn.registry import cache as _cache
from reyn.registry.client import _dedup_by_latest
from reyn.registry.models import server_info_from_raw

# Hardcoded URL — see module docstring for the rationale.
_BASE_URL = "https://registry.modelcontextprotocol.io"

# urlopen timeout in seconds. 10s covers the canonical registry's p99
# without leaving steps hanging if the network is wedged.
_HTTP_TIMEOUT = 10.0

_USER_AGENT = "reyn/1.0 (safe.mcp.registry)"


class RegistryError(RuntimeError):
    """Raised when the registry response is non-2xx or cannot be parsed."""


def _http_get_json(url: str) -> dict:
    """GET ``url`` and JSON-parse the body. Raises :class:`RegistryError` on
    any failure (HTTP non-2xx, transport error, JSON parse error)."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:
        raise RegistryError(
            f"Registry returned HTTP {exc.code} for {url}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface everything as RegistryError
        raise RegistryError(f"Registry transport error for {url}: {exc}") from exc

    if status >= 400:
        raise RegistryError(f"Registry returned HTTP {status} for {url}")

    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryError(f"Registry JSON parse error for {url}: {exc}") from exc


def _info_to_dict(info: Any) -> dict:
    """Convert a :class:`reyn.registry.models.ServerInfo` to the public dict shape."""
    return {
        "name": info.name,
        "description": info.description,
        "repo_url": info.repository_url,
        "runtime_hint": info.runtime_hint,
    }


def _candidates_from_payload(payload: dict) -> list[dict]:
    """Convert a registry search payload into the public list[dict] shape.

    Applies :func:`_dedup_by_latest` to collapse multiple versions of the
    same server down to the newest, then reshapes each entry via
    :func:`server_info_from_raw`.
    """
    raw_entries = payload.get("servers", []) or []
    deduped = _dedup_by_latest(raw_entries)
    out: list[dict] = []
    for entry in deduped:
        info = server_info_from_raw(entry)
        if info.name:
            out.append(_info_to_dict(info))
    return out


def search(query: str, *, limit: int = 20) -> list[dict]:
    """Search the MCP registry for servers matching ``query``.

    Returns a list of result dicts (= newest version per server name,
    other dups removed). Empty list when the query yields no results.
    Raises :class:`RegistryError` on registry / network failure with a
    descriptive message; callers that want fail-soft behaviour should
    catch it and degrade to stale cache / empty result on their side.

    Caching: search results are cached on disk for 24 hours under
    ``~/.reyn/registry-cache/``. Subsequent identical queries within
    the TTL return the cached payload without hitting the network.
    """
    if not query:
        return []
    cache_key = f"search:{query}:{limit}"

    cached = _cache.get(cache_key)
    if cached is not None:
        return _candidates_from_payload(cached)

    qs = urllib.parse.urlencode({"search": query, "limit": str(limit)})
    url = f"{_BASE_URL}/v0.1/servers?{qs}"
    data = _http_get_json(url)
    _cache.set(cache_key, data)
    return _candidates_from_payload(data)


def lookup(server_id: str) -> dict | None:
    """Return the registry entry for the exact ``server_id``, or None.

    ``server_id`` is the registry-canonical name (= ``namespace/server-name``,
    e.g. ``io.github.modelcontextprotocol/server-filesystem``).

    The lookup hits ``/v0.1/servers/{id}/versions/latest`` and reshapes
    the response into the same dict shape as :func:`search`. Returns
    None when the registry replies 404. Raises :class:`RegistryError`
    on other failures (= transport error, parse error, 5xx).

    Caching: 24-hour disk cache keyed by ``server_id``.
    """
    if not server_id:
        return None
    cache_key = f"server:{server_id}"

    cached = _cache.get(cache_key)
    if cached is not None:
        return _info_to_dict(server_info_from_raw(cached))

    url = f"{_BASE_URL}/v0.1/servers/{urllib.parse.quote(server_id, safe='')}/versions/latest"
    try:
        data = _http_get_json(url)
    except RegistryError as exc:
        # 404 = not found → return None instead of raising.
        if "HTTP 404" in str(exc):
            return None
        raise

    _cache.set(cache_key, data)
    return _info_to_dict(server_info_from_raw(data))
