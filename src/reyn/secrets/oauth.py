"""OAuth token lifecycle — FP-0016 Component B.

Adds value-typed OAuth credentials on top of the existing flat
``secrets.env`` static-key store. The tokens live in
``~/.reyn/oauth_tokens.json`` (chmod 600); ``get_valid_token`` lazily
refreshes any token within 60 seconds of expiry via the standard
RFC 6749 §6 ``grant_type=refresh_token`` POST, emits P6 events
(``token_refreshed`` / ``token_refresh_failed``), and serialises
concurrent calls per key via an asyncio lock.

Why a separate file: ``secrets.env`` is a flat dotenv text file used
for static keys (e.g. ``OPENAI_API_KEY``) that ``${VAR}``
interpolation reads through ``os.environ``. OAuth tokens have
multiple fields plus a refresh lifecycle, which fits a structured
JSON store better than embedded JSON in dotenv values.

Out of scope (= deferred to Component C):
- ``reyn auth login`` device-grant CLI (RFC 8628). This module exposes
  ``save_oauth_token`` so the CLI can land here when implemented;
  Component B only covers the refresh path.
- ``${secret:NAME}`` interpolation syntax. ``get_valid_token`` is the
  public surface for now; integration into ``expand_env`` ships later
  when MCP / A2A request paths gain async resolution.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Refresh buffer: if a token expires within this window we refresh
# proactively so the caller never sees a 401. RFC 6749 §6 doesn't
# mandate a value; 60s is the practical lower bound for an OAuth
# round-trip plus clock skew.
_REFRESH_BUFFER_SECONDS = 60

# Default OAuth refresh HTTP timeout. Token endpoints are usually fast
# (< 1s); 10s is generous for adversarial network conditions while
# still capping the per-call latency budget.
_REFRESH_TIMEOUT_SECONDS = 10.0


class OAuthRefreshError(RuntimeError):
    """Raised when the refresh endpoint failed and re-auth is needed.

    Carries a ``re_auth_required`` flag so callers can disambiguate
    "transient network error, please retry" from "the refresh token is
    revoked, please re-run reyn auth login".
    """

    def __init__(
        self,
        message: str,
        *,
        re_auth_required: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.re_auth_required = re_auth_required
        self.status_code = status_code


@dataclass
class OAuthToken:
    """A single OAuth access token with refresh metadata (RFC 6749).

    Stored verbatim in ``~/.reyn/oauth_tokens.json``. The ``expires_at``
    timestamp is ISO 8601 with a timezone (= aware) so cross-machine
    transfer is unambiguous. ``scopes`` is a list of OAuth scope
    strings; the order is preserved for audit-readable round-tripping.
    """

    access_token: str
    refresh_token: str
    token_uri: str
    client_id: str
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)
    # Public OAuth clients (= installed apps, mobile, CLI) often omit
    # ``client_secret``. RFC 6749 §2.3.1 permits this for "public"
    # client types. Store as Optional so device-grant flows work
    # without a secret while confidential clients carry one.
    client_secret: str | None = None

    def is_expired(self, *, buffer_seconds: int = _REFRESH_BUFFER_SECONDS) -> bool:
        """Return True when the token is within the refresh buffer."""
        if self.expires_at.tzinfo is None:
            # Defensive: an aware-only contract is documented above, but
            # a naive timestamp would otherwise raise on comparison.
            return True
        now = datetime.now(timezone.utc)
        threshold = self.expires_at - timedelta(seconds=buffer_seconds)
        return now >= threshold

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSON storage (ISO 8601 expires_at)."""
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthToken":
        """Deserialise from the JSON storage layout."""
        raw_expires = data["expires_at"]
        if isinstance(raw_expires, str):
            expires_at = datetime.fromisoformat(raw_expires)
        elif isinstance(raw_expires, datetime):
            expires_at = raw_expires
        else:
            raise ValueError(
                f"expires_at must be ISO 8601 string or datetime, "
                f"got {type(raw_expires).__name__}"
            )
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            token_uri=data["token_uri"],
            client_id=data["client_id"],
            expires_at=expires_at,
            scopes=list(data.get("scopes") or []),
            client_secret=data.get("client_secret"),
        )


def _default_oauth_path() -> Path:
    """Return the OAuth token store path (env override honoured)."""
    override = os.environ.get("REYN_OAUTH_TOKENS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".reyn" / "oauth_tokens.json"


def _read_store(path: Path) -> dict[str, dict[str, Any]]:
    """Load the JSON map; missing file → empty dict."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.warn(
            f"Could not read OAuth token store at {path}: {exc}",
            UserWarning,
            stacklevel=3,
        )
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"OAuth token store at {path} is not valid JSON ({exc}); "
            "ignoring and continuing with an empty store.",
            UserWarning,
            stacklevel=3,
        )
        return {}
    if not isinstance(data, dict):
        warnings.warn(
            f"OAuth token store at {path} must be a JSON object, "
            f"got {type(data).__name__}; ignoring.",
            UserWarning,
            stacklevel=3,
        )
        return {}
    return data


def _write_store(path: Path, data: dict[str, dict[str, Any]]) -> None:
    """Write the JSON map and enforce chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Best-effort; load_oauth_token will warn if a later check fails.


def load_oauth_token(key: str, *, path: Path | None = None) -> OAuthToken | None:
    """Read a single OAuth token by key. Missing key → None."""
    store_path = path if path is not None else _default_oauth_path()
    _check_permissions(store_path)
    data = _read_store(store_path)
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.warn(
            f"OAuth token {key!r} is not an object; ignoring.",
            UserWarning,
            stacklevel=2,
        )
        return None
    try:
        return OAuthToken.from_dict(raw)
    except (KeyError, ValueError) as exc:
        warnings.warn(
            f"OAuth token {key!r} is malformed ({exc}); ignoring.",
            UserWarning,
            stacklevel=2,
        )
        return None


def save_oauth_token(
    key: str, token: OAuthToken, *, path: Path | None = None,
) -> None:
    """Persist or replace an OAuth token under *key*."""
    if not key:
        raise ValueError("OAuth token key must not be empty")
    store_path = path if path is not None else _default_oauth_path()
    data = _read_store(store_path)
    data[key] = token.to_dict()
    _write_store(store_path, data)


def list_oauth_token_keys(*, path: Path | None = None) -> list[str]:
    """Return the keys present in the store (preserves insertion order)."""
    store_path = path if path is not None else _default_oauth_path()
    data = _read_store(store_path)
    return list(data.keys())


def clear_oauth_token(key: str, *, path: Path | None = None) -> bool:
    """Remove a key from the store. Returns True iff something was removed."""
    store_path = path if path is not None else _default_oauth_path()
    data = _read_store(store_path)
    if key not in data:
        return False
    del data[key]
    _write_store(store_path, data)
    return True


def _check_permissions(path: Path) -> None:
    """Warn + auto-fix when the OAuth store is group/world-readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & stat.S_IROTH or mode & stat.S_IRGRP:
        warnings.warn(
            f"{path} is readable by group/others (mode {oct(mode & 0o777)}); "
            "auto-fixing to 600. Review access controls on this machine.",
            UserWarning,
            stacklevel=3,
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass


# ── refresh lifecycle ───────────────────────────────────────────────────────

# Per-key asyncio lock so concurrent get_valid_token calls for the same
# token serialise (= only one HTTP refresh, the rest wait for the
# refreshed token). Keys map to locks lazily; the dict itself is
# accessed from the asyncio event loop only so a simple module dict
# is safe without an additional lock.
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _REFRESH_LOCKS:
        _REFRESH_LOCKS[key] = asyncio.Lock()
    return _REFRESH_LOCKS[key]


async def _post_refresh(
    token: OAuthToken,
    *,
    http_client: Any = None,
    timeout: float = _REFRESH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Issue the RFC 6749 §6 refresh POST and return the JSON response.

    ``http_client`` is an httpx.AsyncClient-compatible instance — tests
    pass an httpx.AsyncClient configured with a MockTransport so we
    avoid network I/O without introducing MagicMock.
    """
    import httpx

    if http_client is None:
        http_client = httpx.AsyncClient(timeout=timeout)
        owns_client = True
    else:
        owns_client = False

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": token.client_id,
    }
    if token.client_secret:
        payload["client_secret"] = token.client_secret

    try:
        resp = await http_client.post(
            token.token_uri,
            data=payload,
            headers={"Accept": "application/json"},
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if resp.status_code >= 500:
        raise OAuthRefreshError(
            f"OAuth provider returned {resp.status_code}; transient — retry.",
            re_auth_required=False,
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        # 400 invalid_grant / 401 invalid_client → refresh token rejected.
        raise OAuthRefreshError(
            f"OAuth refresh rejected with HTTP {resp.status_code}; "
            "re-auth required (run `reyn auth login <provider>`).",
            re_auth_required=True,
            status_code=resp.status_code,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise OAuthRefreshError(
            f"OAuth provider returned non-JSON body: {exc}",
            re_auth_required=False,
            status_code=resp.status_code,
        ) from exc


def _token_from_refresh_response(
    old: OAuthToken, body: dict[str, Any],
) -> OAuthToken:
    """Build a new OAuthToken from a refresh response, preserving fields."""
    access_token = body.get("access_token")
    if not access_token:
        raise OAuthRefreshError(
            "OAuth refresh response missing 'access_token'.",
            re_auth_required=False,
        )
    # RFC 6749 §6: the response MAY include a new refresh_token. If
    # omitted the caller keeps reusing the old one.
    refresh_token = body.get("refresh_token") or old.refresh_token
    expires_in = body.get("expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        # Default to 1 hour when the server doesn't say (RFC 6749 §4.2.2
        # leaves it provider-defined).
        expires_in = 3600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    # ``scope`` in the response is a space-separated string per RFC
    # 6749 §3.3; preserve the prior scopes when omitted.
    raw_scope = body.get("scope")
    if isinstance(raw_scope, str) and raw_scope.strip():
        scopes = raw_scope.split()
    else:
        scopes = list(old.scopes)
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        token_uri=old.token_uri,
        client_id=old.client_id,
        expires_at=expires_at,
        scopes=scopes,
        client_secret=old.client_secret,
    )


async def get_valid_token(
    key: str,
    *,
    events: Any = None,  # EventLog | None — avoid import cycle
    path: Path | None = None,
    http_client: Any = None,
) -> str:
    """Return a known-valid access token for *key*, refreshing if needed.

    Behaviour:
      1. Load the token. Missing key → ``KeyError``.
      2. If the token has >60 s of remaining validity → return its
         ``access_token`` verbatim.
      3. Else acquire the per-key lock and re-check (= the holder of
         the lock may have already refreshed). Then issue the RFC 6749
         refresh POST, save the new token, emit ``token_refreshed``,
         and return the new ``access_token``.
      4. On refresh failure: emit ``token_refresh_failed`` with
         ``re_auth_required`` and re-raise ``OAuthRefreshError``.

    ``events`` is an optional EventLog; when provided, P6 events stamp
    the audit trail. ``http_client`` is for tests (httpx.AsyncClient
    with a MockTransport).
    """
    token = load_oauth_token(key, path=path)
    if token is None:
        raise KeyError(f"No OAuth token stored under key {key!r}")
    if not token.is_expired():
        return token.access_token

    async with _get_lock(key):
        # Re-check after lock acquisition — another coroutine may have
        # refreshed while we waited.
        token = load_oauth_token(key, path=path)
        if token is None:
            raise KeyError(f"OAuth token {key!r} disappeared during refresh")
        if not token.is_expired():
            return token.access_token

        try:
            body = await _post_refresh(token, http_client=http_client)
            new_token = _token_from_refresh_response(token, body)
        except OAuthRefreshError as exc:
            if events is not None:
                try:
                    events.emit(
                        "token_refresh_failed",
                        key=key,
                        error=str(exc),
                        re_auth_required=exc.re_auth_required,
                        status_code=exc.status_code,
                    )
                except Exception:  # noqa: BLE001 — emit must not mask the raise
                    pass
            raise

        save_oauth_token(key, new_token, path=path)
        if events is not None:
            try:
                events.emit(
                    "token_refreshed",
                    key=key,
                    expires_at=new_token.expires_at.isoformat(),
                    scopes=list(new_token.scopes),
                )
            except Exception:  # noqa: BLE001 — emit failure must not break refresh
                pass
        return new_token.access_token
