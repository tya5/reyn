"""Safe-mode MCP server registry lookup (FP-0042 Phase 2.4).

Exposes ``search(query, limit)`` and ``lookup(server_id)`` to safe-mode
python steps. Internally batches HTTP GET against the MCP server
registry, caches responses on disk (``~/.reyn/registry-cache/``),
parses + dedups the registry envelope, and returns plain
JSON-serialisable dicts.

Registry URL resolution
-----------------------

The base URL is resolved from the ``REYN_MCP_REGISTRY_URL`` environment
variable, falling back to the official registry at
``https://registry.modelcontextprotocol.io`` when the env var is unset.
This mirrors the resolution chain used by
:class:`reyn.registry.client.RegistryClient` (= the op-handler-side
async client), so an operator who points reyn at a private / corporate
registry sees both code paths use the same URL.

Threat model
------------

Registry lookup remains an *ambient* operation from the skill author's
perspective — there is no per-skill ``http.get`` declaration required
for this module's surface. The URL is operator-trusted via the env
var; the operator setting ``REYN_MCP_REGISTRY_URL`` is what carries the
authorisation, not the skill code that calls in. This matches how
``time`` / ``random`` / file-system locale are treated.

Multi-registry support (= ``reyn.yaml mcp.registries: [...]`` list) is
not yet wired through this module. Today only the single-URL env var
override works; the list-form config is a future enhancement.

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
import os
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

_DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"


def _base_url() -> str:
    """Resolve the registry base URL.

    Reads ``REYN_MCP_REGISTRY_URL`` from the environment (= operator-
    trusted single-URL override, same chain as
    :func:`reyn.registry.client._base_url`). Falls back to the official
    public registry when the env var is unset.
    """
    return os.environ.get("REYN_MCP_REGISTRY_URL", _DEFAULT_BASE_URL).rstrip("/")

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
    url = f"{_base_url()}/v0.1/servers?{qs}"
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

    url = f"{_base_url()}/v0.1/servers/{urllib.parse.quote(server_id, safe='')}/versions/latest"
    try:
        data = _http_get_json(url)
    except RegistryError as exc:
        # 404 = not found → return None instead of raising.
        if "HTTP 404" in str(exc):
            return None
        raise

    _cache.set(cache_key, data)
    return _info_to_dict(server_info_from_raw(data))
