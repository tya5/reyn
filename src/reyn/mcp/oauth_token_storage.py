"""MCP OAuth token storage — #2597 slice ④ (OAuth 2.1 + Streamable HTTP completion).

FastMCP's browser-based OAuth helper (``fastmcp.client.auth.OAuth`` — verified
against the installed fastmcp 3.4.2 source at
``fastmcp/client/auth/oauth.py``) does NOT implement its own bare
``mcp.client.auth.TokenStorage`` (get_tokens/set_tokens/get_client_info/
set_client_info) as the module docstring of the umbrella issue originally
assumed. Instead ``OAuth(..., token_storage=...)`` takes a
``key_value.aio.protocols.AsyncKeyValue``-conforming store (the
``key-value-aio`` package's generic async KV protocol: ``get``/``put``/
``delete``/``ttl`` + ``*_many`` bulk variants, each keyed by ``(key,
collection)``) and wraps it internally in its own
``TokenStorageAdapter(async_key_value, server_url)`` — a ``PydanticAdapter``
that serialises ``mcp.shared.auth.OAuthToken`` / ``OAuthClientInformationFull``
to/from plain JSON-safe dicts before calling ``get``/``put`` on whatever store
we hand it, under two collections (``"mcp-oauth-token"``,
``"mcp-oauth-client-info"``) plus one bare key (``"<url>/token_expiry"``,
collection ``"mcp-oauth-token-expiry"``) it manages directly on the KV store
for the ABSOLUTE access-token expiry (added upstream to fix a stale-relative-
``expires_in`` bug on reload). :class:`MCPOAuthTokenStorage` below is reyn's
implementation of that ``AsyncKeyValue`` protocol — NOT FastMCP's assumed
``TokenStorage`` ABC — verified by reading ``fastmcp/client/auth/oauth.py``
and ``key_value/aio/protocols/key_value.py`` directly (see this module's PR
for the read-out).

Storage location — the reyn-dir-layout "outside" bucket (see
``docs/reference/runtime/reyn-dir-layout.md``): tokens land in the SAME
``~/.reyn/oauth_tokens.json`` (chmod 600) that reyn's existing RFC 8628
device-grant store (:mod:`reyn.security.secrets.oauth`, FP-0016 Component
B/C — merged before this slice) already reads/writes. This module reuses
THAT module's ``_read_store``/``_write_store``/``_default_oauth_path``
helpers (single-file read-modify-write + chmod 600 enforcement) rather than
introducing a second on-disk JSON-store implementation — "grep existing
mechanism first" — and namespaces every entry it writes under an
``"mcp:"``-prefixed compound key (``mcp:<collection>::<key>``) so MCP OAuth
entries can never collide with the device-grant module's provider-keyed
entries in the same file. Neither store is under ``.reyn/`` (project-scoped
recovery-core) — both are ``~/.reyn/`` (operator/user-owned, outside bucket):
never written through a WAL-emitting op, never captured by rewind/PITR, and
this module contains no logging of token VALUES (only keys/URLs ever appear
in any warning/error text).

Headless / no-token graceful failure: FastMCP's ``OAuth`` opens a browser +
a localhost callback server to complete the FIRST authorization — that only
works with an attached interactive session. :func:`has_stored_token` lets
:mod:`reyn.mcp.client` check, BEFORE constructing the transport, whether a
usable token is already cached for a given MCP server URL; if not, and the
caller is running non-interactively, ``client.py`` raises a clear
``MCPError`` instead of letting FastMCP hang (bounded only by its own
``callback_timeout``, default 300s) waiting for a browser round-trip nobody
can complete.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reyn.security.secrets.oauth import (
    _default_oauth_path,
    _read_store,
    _write_store,
)

# Same collection + key-shape FastMCP's ``TokenStorageAdapter._get_token_cache_key``
# uses (``f"{server_url}/tokens"``, collection ``"mcp-oauth-token"``) — verified
# against the installed fastmcp 3.4.2 source. Duplicated here (not imported) so
# :func:`has_stored_token` can answer the "do we already have a token" question
# with a plain synchronous file read, without instantiating FastMCP's OAuth /
# PydanticAdapter machinery just to ask.
_TOKEN_COLLECTION = "mcp-oauth-token"


def _mcp_compound_key(key: str, collection: str | None) -> str:
    """Namespace ``(key, collection)`` into the single flat dict the shared
    ``oauth_tokens.json`` file stores, prefixed so MCP OAuth entries can never
    collide with :mod:`reyn.security.secrets.oauth`'s device-grant keys."""
    return f"mcp:{collection or '_default'}::{key}"


def _server_token_key(mcp_url: str) -> str:
    """Mirror FastMCP's ``TokenStorageAdapter._get_token_cache_key`` exactly:
    the URL is right-stripped of a trailing slash first (verified against
    ``OAuth._bind``, which does ``mcp_url = mcp_url.rstrip("/")`` before
    constructing its ``TokenStorageAdapter``)."""
    return f"{mcp_url.rstrip('/')}/tokens"


def has_stored_token(mcp_url: str, *, path: Path | None = None) -> bool:
    """Return True iff a (not-yet-expired-by-our-own-TTL) OAuth token is
    already cached for ``mcp_url``. Used by :mod:`reyn.mcp.client` to decide,
    BEFORE opening a transport, whether a non-interactive caller can proceed
    without hitting FastMCP's browser flow. Never raises; a corrupt/missing
    store answers False (= "no usable token yet", the conservative default —
    same posture as :class:`~reyn.mcp.client.MCPClient.supports`)."""
    store_path = path if path is not None else _default_oauth_path()
    data = _read_store(store_path)
    entry = data.get(_mcp_compound_key(_server_token_key(mcp_url), _TOKEN_COLLECTION))
    if not isinstance(entry, dict):
        return False
    expires_at = entry.get("_expires_at")
    if expires_at is not None and time.time() >= expires_at:
        return False
    return isinstance(entry.get("_value"), dict)


class MCPOAuthTokenStorage:
    """``key_value.aio.protocols.AsyncKeyValue``-conforming store FastMCP's
    ``OAuth(token_storage=...)`` accepts (see module docstring for the exact
    verified contract). Backed by the shared ``~/.reyn/oauth_tokens.json``
    (outside bucket, chmod 600) via :mod:`reyn.security.secrets.oauth`'s
    ``_read_store``/``_write_store`` helpers — a full-file read-modify-write
    per call, which is fine at OAuth's write frequency (login + occasional
    refresh, not a hot path).

    Never logs token values: every method here only ever formats *keys* /
    *collection names* into anything user-visible (there is nothing
    user-visible in this class at all — it's pure file I/O).
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path if path is not None else _default_oauth_path()

    def _load(self) -> dict[str, Any]:
        return _read_store(self._path)

    def _save(self, data: dict[str, Any]) -> None:
        _write_store(self._path, data)

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        value, _ttl = await self.ttl(key, collection=collection)
        return value

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        entry = self._load().get(_mcp_compound_key(key, collection))
        if not isinstance(entry, dict):
            return None, None
        expires_at = entry.get("_expires_at")
        value = entry.get("_value")
        if not isinstance(value, dict):
            return None, None
        if expires_at is not None:
            remaining = expires_at - time.time()
            if remaining <= 0:
                return None, None
            return dict(value), remaining
        return dict(value), None

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        data = self._load()
        data[_mcp_compound_key(key, collection)] = {
            "_value": dict(value),
            "_expires_at": (time.time() + float(ttl)) if ttl is not None else None,
        }
        self._save(data)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        data = self._load()
        compound = _mcp_compound_key(key, collection)
        if compound not in data:
            return False
        del data[compound]
        self._save(data)
        return True

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        return [await self.get(k, collection=collection) for k in keys]

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        return [await self.ttl(k, collection=collection) for k in keys]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        data = self._load()
        expires_at = (time.time() + float(ttl)) if ttl is not None else None
        for k, v in zip(keys, values):
            data[_mcp_compound_key(k, collection)] = {
                "_value": dict(v),
                "_expires_at": expires_at,
            }
        self._save(data)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        data = self._load()
        removed = 0
        for k in keys:
            compound = _mcp_compound_key(k, collection)
            if compound in data:
                del data[compound]
                removed += 1
        if removed:
            self._save(data)
        return removed
