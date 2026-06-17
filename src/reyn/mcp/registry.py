"""Safe-mode MCP server registry lookup (FP-0042 Phase 2.4).

Exposes ``search(query, limit)`` and ``lookup(server_id)`` to safe-mode
python steps. Internally batches HTTP GET against the MCP server
registry, caches responses on disk (``~/.reyn/registry-cache/``),
parses + dedups the registry envelope, and returns plain
JSON-serialisable dicts.

Registry URL resolution
-----------------------

The base URL list is resolved in priority order:

1. ``REYN_MCP_REGISTRY_URLS`` (plural) — comma-separated list, used
   for multi-registry fallback (e.g. ``private,public``).
2. ``REYN_MCP_REGISTRY_URL`` (singular) — single URL, legacy alias
   treated as a one-item list.
3. Default ``https://registry.modelcontextprotocol.io``.

When set in ``reyn.yaml`` under ``mcp.registries: [...]``, the
config-loader exports the list into ``REYN_MCP_REGISTRY_URLS`` (only
if neither env var is already set, so explicit operator overrides
win). The subprocess running safe-mode python steps inherits the env
var from the parent process automatically.

This mirrors the resolution chain used by
:class:`reyn.core.registry.client.RegistryClient`, so both surfaces — the
async op-handler client and this safe-mode skill-internal lookup —
agree on which registries to try and in what order.

Multi-registry semantics
------------------------

``lookup(server_id)`` tries each URL in order, returning the first
non-404 hit. A 404 from one registry falls through to the next.
``search(query)`` queries each URL in order and returns the first
non-empty result list (= "private first, public fallback" semantics).
A ``RegistryError`` from one URL falls through to the next; if every
URL fails the final error is re-raised.

Threat model
------------

Registry lookup remains an *ambient* operation from the skill author's
perspective — there is no per-skill ``http.get`` declaration required
for this module's surface. The URLs are operator-trusted via the env
var / config; the operator setting them is what carries the
authorisation, not the skill code that calls in.

Internal layering
-----------------

This module is reyn-package internal code (= not subject to the
safe-mode AST validator). It is free to import urllib / reyn.core.registry
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
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Internal reuse of the non-safe-namespaced helpers. These are pure
# (= dedup is a list transform; server_info_from_raw is a dict reshape)
# or scoped to reyn-internal disk cache. None of them are reachable from
# user code through this safe namespace surface.
from reyn.core.registry import cache as _cache
from reyn.core.registry.client import _dedup_by_latest
from reyn.core.registry.models import server_info_from_raw

_DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"


def registry_urls() -> list[str]:
    """Resolve the ordered list of registry URLs to try.

    Resolution priority (= same chain as
    :func:`reyn.core.registry.client._base_urls`):

    1. ``REYN_MCP_REGISTRY_URLS`` (plural) — comma-separated list.
    2. ``REYN_MCP_REGISTRY_URL`` (singular, legacy) — single-item list.
    3. Default single-item list with the public registry.

    Each URL is trimmed and trailing-slash-normalised. Empty entries
    are dropped. When both env vars are set, the plural form wins.
    """
    plural = os.environ.get("REYN_MCP_REGISTRY_URLS")
    if plural:
        urls = [u.strip().rstrip("/") for u in plural.split(",")]
        return [u for u in urls if u]
    singular = os.environ.get("REYN_MCP_REGISTRY_URL")
    if singular:
        return [singular.strip().rstrip("/")]
    return [_DEFAULT_BASE_URL]


def base_url() -> str:
    """Return the first registry URL (= preserved for backward compat).

    Callers that only need a single URL (= legacy paths, tests) can
    keep using this; the multi-URL iteration happens inside ``search``
    and ``lookup`` via :func:`registry_urls`.
    """
    return registry_urls()[0]

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
    """Convert a :class:`reyn.core.registry.models.ServerInfo` to the public dict shape."""
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

    Iterates the resolved registry URL list (= ``registry_urls()``)
    and returns the first non-empty result list — "private first,
    public fallback" semantics. A :class:`RegistryError` from one URL
    falls through to the next; if every URL fails the final error is
    re-raised.

    Caching: results are cached on disk for 24 hours under
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
    last_error: RegistryError | None = None
    for base in registry_urls():
        try:
            data = _http_get_json(f"{base}/v0.1/servers?{qs}")
        except RegistryError as exc:
            last_error = exc
            continue
        candidates = _candidates_from_payload(data)
        if candidates:
            _cache.set(cache_key, data)
            return candidates
        # Empty result at this registry — fall through to next URL.
    if last_error is not None:
        raise last_error
    return []


def lookup(server_id: str) -> dict | None:
    """Return the registry entry for the exact ``server_id``, or None.

    Iterates the resolved registry URL list (= ``registry_urls()``):
    a 404 from one URL falls through to the next; a non-404 hit
    returns immediately. Returns None when every URL replies 404.
    Raises :class:`RegistryError` if the final URL fails with a
    non-404 error after all others were 404.

    Caching: 24-hour disk cache keyed by ``server_id``.
    """
    if not server_id:
        return None
    cache_key = f"server:{server_id}"

    cached = _cache.get(cache_key)
    if cached is not None:
        return _info_to_dict(server_info_from_raw(cached))

    encoded_id = urllib.parse.quote(server_id, safe="")
    last_error: RegistryError | None = None
    for base in registry_urls():
        try:
            data = _http_get_json(
                f"{base}/v0.1/servers/{encoded_id}/versions/latest"
            )
        except RegistryError as exc:
            # 404 → not found on this URL, try the next one. Other
            # errors (= transport, parse, 5xx) are remembered as
            # last_error in case all URLs fail.
            if "HTTP 404" in str(exc):
                continue
            last_error = exc
            continue
        _cache.set(cache_key, data)
        return _info_to_dict(server_info_from_raw(data))

    # Every URL returned an error (404 or otherwise).
    if last_error is not None:
        raise last_error
    return None
