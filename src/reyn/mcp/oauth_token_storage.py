"""MCP OAuth token storage — #2597 slice ④ (OAuth 2.1 + Streamable HTTP),
rewritten for #3698 stage 2 / #4282 (fastmcp's ``OAuth`` retired in favour of
the official ``mcp`` SDK's ``mcp.client.auth.OAuthClientProvider`` directly).

:class:`MCPOAuthTokenStorage` below implements the official SDK's OWN
``mcp.client.auth.TokenStorage`` protocol (``get_tokens``/``set_tokens``/
``get_client_info``/``set_client_info`` — verified by reading the installed
``mcp`` SDK's ``mcp/client/auth/oauth2.py`` directly) — NOT fastmcp's
``key_value.aio.protocols.AsyncKeyValue`` shape this module used pre-#4282.

Why the double adapter this module previously carried is gone: fastmcp's
``OAuth(token_storage=...)`` wanted an ``AsyncKeyValue``-conforming store
(``get``/``put``/``delete``/``ttl`` + ``*_many`` bulk variants, keyed by
``(key, collection)``) that IT wrapped internally in its own
``TokenStorageAdapter`` before handing the (simpler) ``TokenStorage`` shape
down to ``OAuthClientProvider``. Now that ``OAuthClientProvider`` is
constructed directly (#4282: fastmcp.Client is no longer built for ANY
transport, OAuth included — see :mod:`reyn.mcp.client`'s module docstring),
nothing in reyn's process ever asks for the ``AsyncKeyValue`` shape again —
carrying both would be dead code for a caller that no longer exists.
``TokenStorage`` is also structurally simpler: ONE instance is bound to ONE
``server_url`` (passed to ``OAuthClientProvider.__init__``, not to each
storage call), so there is no ``(key, collection)`` parameter to plumb here
either.

Storage location — unchanged, the reyn-dir-layout "outside" bucket (see
``docs/reference/runtime/reyn-dir-layout.md``): tokens land in the SAME
``~/.reyn/oauth_tokens.json`` (chmod 600) that reyn's existing RFC 8628
device-grant store (:mod:`reyn.security.secrets.oauth`, FP-0016 Component
B/C) already reads/writes, reusing THAT module's ``_read_store``/
``_write_store``/``_default_oauth_path`` helpers. Neither store is under
``.reyn/`` (project-scoped recovery-core) — both are ``~/.reyn/``
(operator/user-owned, outside bucket): never written through a WAL-emitting
op, never captured by rewind/PITR, and this module logs no token VALUES
(only keys/URLs ever appear in any warning/error text).

Key scheme — a NEW, disjoint namespace from the pre-#4282 AsyncKeyValue
compound-key scheme (``mcp:<collection>::<key>``): every key here is
prefixed ``mcp-oauth2:``. This is a deliberate clean break, not an oversight
— per the owner's standing "no backward-compat, no migration" ruling
(CLAUDE.md), an entry written under the OLD scheme is simply never looked up
under the NEW one; no migration script is written, and reauthentication
via the browser flow is the expected (and only) recovery path. **This is
NOT a silent-failure risk for a caller who upgrades reyn with an old token
file on disk**: the OLD entries just don't exist under the new keys, so
:func:`has_stored_token` reports False and the normal
"needs authentication" path runs — the same path a first-ever run takes.
The failure mode this module DOES guard against explicitly (per lead-coder's
review condition on #4282) is a NEW-scheme entry whose stored JSON shape no
longer round-trips through ``OAuthToken``/``OAuthClientInformationFull``
(e.g. a FUTURE reyn version changes what it stores) — :meth:`get_tokens`/
:meth:`get_client_info` catch that specific case and emit a clear
``logging.warning`` naming the server + the fact that re-authentication is
needed, rather than letting a raw ``pydantic.ValidationError`` propagate
from deep inside the SDK's OAuth flow, or (the worse alternative) silently
swallowing it and returning None with no trace at all.

Expiry tracking — the official SDK's own ``OAuthContext`` does NOT persist
``token_expiry_time`` across a process restart (verified by reading
``mcp/client/auth/oauth2.py``'s ``_initialize()``: it restores
``current_tokens`` from storage but never calls ``update_token_expiry()``
on the restored value) — a reloaded token is treated as valid until a real
401 triggers the SDK's own reactive full-reauth path. That is the SDK's own
design choice and not this module's to work around. This module tracks its
OWN best-effort absolute expiry (``time.time() + expires_in`` at write time)
SEPARATELY, for exactly one purpose: :func:`has_stored_token`'s headless
pre-flight check in :mod:`reyn.mcp.client`, so a non-interactive caller with
an obviously-expired cached token gets reyn's own clear
"run interactively to re-authenticate" ``MCPError`` immediately, instead of
proceeding to a request that will 401 and then have the SDK attempt a
browser-flow re-auth that a headless caller can never complete.

Headless / no-token graceful failure: :func:`has_stored_token` lets
:mod:`reyn.mcp.client` check, BEFORE constructing the transport, whether a
usable token is already cached for a given MCP server URL; if not, and the
caller is running non-interactively, ``client.py`` raises a clear
``MCPError`` instead of letting the OAuth flow hang waiting for a browser
round-trip nobody can complete.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from reyn.security.secrets.oauth import (
    _default_oauth_path,
    _read_store,
    _write_store,
)

logger = logging.getLogger(__name__)

# Disjoint from the pre-#4282 AsyncKeyValue compound-key prefix ("mcp:") —
# see module docstring's "Key scheme" section for why this is a deliberate
# clean break, not an oversight.
_KEY_PREFIX = "mcp-oauth2"


def _tokens_key(server_url: str) -> str:
    return f"{_KEY_PREFIX}:{server_url.rstrip('/')}:tokens"


def _client_info_key(server_url: str) -> str:
    return f"{_KEY_PREFIX}:{server_url.rstrip('/')}:client_info"


def has_stored_token(mcp_url: str, *, path: Path | None = None) -> bool:
    """Return True iff a (not-yet-expired-by-our-own-tracking) OAuth token
    is already cached for ``mcp_url``. Never raises; a corrupt/missing store
    or an entry in an unrecognized shape answers False (= "no usable token
    yet", the conservative default — same posture as
    :class:`~reyn.mcp.client.MCPClient.supports`) — the caller then takes
    the normal "needs authentication" path, which is the correct fallback
    either way."""
    store_path = path if path is not None else _default_oauth_path()
    data = _read_store(store_path)
    entry = data.get(_tokens_key(mcp_url))
    if not isinstance(entry, dict):
        return False
    expires_at = entry.get("_expires_at")
    if expires_at is not None and time.time() >= expires_at:
        return False
    value = entry.get("_value")
    return isinstance(value, dict) and bool(value.get("access_token"))


class MCPOAuthTokenStorage:
    """``mcp.client.auth.TokenStorage``-conforming store for ONE MCP server
    (bound to ``server_url`` at construction — matches
    ``OAuthClientProvider.__init__(server_url, ..., storage=...)``'s
    per-instance-per-server contract). Backed by the shared
    ``~/.reyn/oauth_tokens.json`` (outside bucket, chmod 600) via
    :mod:`reyn.security.secrets.oauth`'s ``_read_store``/``_write_store``
    helpers — a full-file read-modify-write per call, which is fine at
    OAuth's write frequency (login + occasional refresh, not a hot path).

    Never logs token VALUES: every log line here only ever formats the
    server URL / key names into anything user-visible.
    """

    def __init__(self, server_url: str, *, path: Path | None = None) -> None:
        self._server_url = server_url
        self._path = path if path is not None else _default_oauth_path()

    def _load(self) -> dict[str, Any]:
        return _read_store(self._path)

    def _save(self, data: dict[str, Any]) -> None:
        _write_store(self._path, data)

    async def get_tokens(self) -> "Any | None":
        """Return the stored ``mcp.shared.auth.OAuthToken``, or None if
        absent / unreadable / no longer valid against the current schema.

        A ``pydantic.ValidationError`` while re-hydrating an on-disk entry
        is caught and logged at WARNING (naming the server + "format
        changed, re-authentication required") rather than propagating raw
        from inside the SDK's OAuth flow, or being silently swallowed —
        see the module docstring's expiry/failure-mode section."""
        entry = self._load().get(_tokens_key(self._server_url))
        if not isinstance(entry, dict):
            return None
        value = entry.get("_value")
        if not isinstance(value, dict):
            return None
        expires_at = entry.get("_expires_at")
        if expires_at is not None and time.time() >= expires_at:
            return None
        from mcp.shared.auth import OAuthToken

        try:
            return OAuthToken.model_validate(value)
        except Exception:
            logger.warning(
                "Stored MCP OAuth token for %r is in an unrecognized format "
                "(schema changed since it was written) — treating as absent; "
                "re-authenticate via the browser flow to refresh it.",
                self._server_url,
            )
            return None

    async def set_tokens(self, tokens: "Any") -> None:
        data = self._load()
        expires_at = (
            time.time() + float(tokens.expires_in)
            if tokens.expires_in is not None
            else None
        )
        data[_tokens_key(self._server_url)] = {
            "_value": tokens.model_dump(mode="json", exclude_none=True),
            "_expires_at": expires_at,
        }
        self._save(data)

    async def get_client_info(self) -> "Any | None":
        """Return the stored ``mcp.shared.auth.OAuthClientInformationFull``,
        or None if absent / unreadable — same defensive posture as
        :meth:`get_tokens`."""
        entry = self._load().get(_client_info_key(self._server_url))
        if not isinstance(entry, dict):
            return None
        from mcp.shared.auth import OAuthClientInformationFull

        try:
            return OAuthClientInformationFull.model_validate(entry)
        except Exception:
            logger.warning(
                "Stored MCP OAuth client registration for %r is in an "
                "unrecognized format (schema changed since it was written) — "
                "treating as absent; dynamic client registration will run again.",
                self._server_url,
            )
            return None

    async def set_client_info(self, client_info: "Any") -> None:
        data = self._load()
        data[_client_info_key(self._server_url)] = client_info.model_dump(
            mode="json", exclude_none=True
        )
        self._save(data)
