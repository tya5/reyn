"""OAuth token lifecycle — FP-0016 Component B + C.

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
from collections.abc import Callable
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


# ── RFC 8628 Device Authorization Grant (Component C) ──────────────────────

# Default polling interval when the server does not specify one (RFC 8628
# §3.2 recommends 5 s as the minimum).
_DEVICE_POLL_INTERVAL_DEFAULT = 5.0

# Default token lifetime when the server omits expires_in (30 minutes,
# the example value from RFC 8628 §3.2).
_DEVICE_EXPIRES_IN_DEFAULT = 1800

# Number of seconds added to the poll interval on a slow_down response
# (RFC 8628 §3.5 mandates "at least 5 seconds").
_SLOW_DOWN_INCREMENT = 5.0

# HTTP POST timeout for device-grant endpoints (generous; auth servers are
# typically fast but enterprise proxies can add latency).
_DEVICE_HTTP_TIMEOUT = 15.0


@dataclass
class OAuthProviderConfig:
    """OAuth 2.0 provider configuration for RFC 8628 device authorization grant.

    Operators define one per provider in reyn.yaml ``auth.providers.<name>``.
    """

    name: str  # provider 名 (= "github", "google", "acme")、display 用
    client_id: str  # provider 側で発行された OAuth client ID
    device_authorization_url: str  # POST 先 — device_code 発行 endpoint
    token_url: str  # POST 先 — access_token polling endpoint
    scopes: list[str] = field(default_factory=list)  # OAuth scope strings
    client_secret: str | None = None  # 公開 client (= installed app) では省略可
    audience: str | None = None  # Auth0 等の API audience 識別子 (optional)


class DeviceGrantError(RuntimeError):
    """Device grant 失敗 (= access_denied / expired_token / 不正 response)."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code  # RFC 8628 §3.5 error_code (= access_denied 等)


async def _post_device_authorization(
    provider: OAuthProviderConfig,
    http_client: Any,
) -> dict[str, Any]:
    """POST to the device_authorization_url and return the parsed JSON body.

    RFC 8628 §3.1: the request carries ``client_id`` and optionally ``scope``
    (space-separated). Returns the JSON object including ``device_code``,
    ``user_code``, ``verification_uri``, ``expires_in``, and ``interval``.
    """
    import httpx

    payload: dict[str, str] = {"client_id": provider.client_id}
    if provider.scopes:
        payload["scope"] = " ".join(provider.scopes)

    resp = await http_client.post(
        provider.device_authorization_url,
        data=payload,
        headers={"Accept": "application/json"},
    )
    if resp.status_code >= 400:
        raise DeviceGrantError(
            f"Device authorization endpoint returned HTTP {resp.status_code} "
            f"for provider {provider.name!r}.",
            error_code="device_authorization_failed",
        )
    try:
        return resp.json()  # type: ignore[no-any-return]
    except ValueError as exc:
        raise DeviceGrantError(
            f"Device authorization endpoint returned non-JSON body: {exc}",
            error_code="device_authorization_failed",
        ) from exc


async def _poll_token(
    provider: OAuthProviderConfig,
    device_code: str,
    http_client: Any,
) -> tuple[int, dict[str, Any]]:
    """POST to the token_url and return (status_code, parsed_body).

    Body per RFC 8628 §3.4: ``grant_type``, ``device_code``, ``client_id``
    (+ optional ``client_secret`` and ``audience``). We return the raw status
    code so the caller can implement the RFC 8628 §3.5 polling state machine
    without re-parsing.
    """
    payload: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        payload["client_secret"] = provider.client_secret
    if provider.audience:
        payload["audience"] = provider.audience

    resp = await http_client.post(
        provider.token_url,
        data=payload,
        headers={"Accept": "application/json"},
    )
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def _parse_token_success(
    body: dict[str, Any],
    *,
    provider: OAuthProviderConfig,
) -> OAuthToken:
    """Build an OAuthToken from a successful RFC 8628 token response body."""
    access_token = body.get("access_token")
    if not access_token:
        raise DeviceGrantError(
            "Token endpoint success response missing 'access_token'.",
            error_code="invalid_response",
        )
    refresh_token: str = body.get("refresh_token") or ""
    expires_in = body.get("expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = _DEVICE_EXPIRES_IN_DEFAULT
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    raw_scope = body.get("scope")
    if isinstance(raw_scope, str) and raw_scope.strip():
        scopes = raw_scope.split()
    else:
        scopes = list(provider.scopes)
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        token_uri=provider.token_url,
        client_id=provider.client_id,
        expires_at=expires_at,
        scopes=scopes,
        client_secret=provider.client_secret,
    )


async def device_grant_flow(
    provider: OAuthProviderConfig,
    *,
    events: Any = None,  # EventLog | None
    http_client: Any = None,  # httpx.AsyncClient | None
    on_user_action: Callable[[dict], None] | None = None,
    poll_interval_override: float | None = None,  # for tests
    deadline_override: float | None = None,  # max wall-clock seconds (for tests)
) -> OAuthToken:
    """RFC 8628 Device Authorization Grant flow.

    Steps:
    1. POST provider.device_authorization_url with client_id + scope
       → response has device_code, user_code, verification_uri,
         verification_uri_complete (optional), interval (default 5s),
         expires_in (default 1800s = 30 min)
    2. Display user_code + verification_uri to user via on_user_action
       callback (= CLI subprocess prints them). When callback is None,
       fall back to print() to stdout.
    3. Emit oauth_login_started event (key=provider.name, device_code
       last 4 chars only for security, verification_uri, expires_at).
    4. Poll provider.token_url with grant_type=
       urn:ietf:params:oauth:grant-type:device_code, client_id,
       device_code (and client_secret + audience if set):
       - HTTP 200 with access_token → SUCCESS: parse and return
         OAuthToken, emit oauth_login_completed.
       - HTTP 400 with error=authorization_pending → user has not yet
         approved; wait interval seconds, poll again.
       - HTTP 400 with error=slow_down → server says back off; add 5s
         to interval, poll again.
       - HTTP 400 with error=access_denied → user denied; raise
         DeviceGrantError(error_code="access_denied").
       - HTTP 400 with error=expired_token → device code expired;
         raise DeviceGrantError(error_code="expired_token").
       - Other HTTP error → raise DeviceGrantError(error_code=
         response_error_code).
    5. After expires_in seconds without success → raise
       DeviceGrantError(error_code="timeout").
    """
    import httpx

    owns_client = http_client is None
    if owns_client:
        http_client = httpx.AsyncClient(timeout=_DEVICE_HTTP_TIMEOUT)

    try:
        # ── Step 1: request device_code ──────────────────────────────────
        auth_resp = await _post_device_authorization(provider, http_client)

        device_code: str = auth_resp.get("device_code", "")
        user_code: str = auth_resp.get("user_code", "")
        verification_uri: str = auth_resp.get("verification_uri", "")
        # verification_uri_complete is optional (RFC 8628 §3.2); pass it
        # through to on_user_action so CLI can display a clickable link.
        verification_uri_complete: str | None = auth_resp.get(
            "verification_uri_complete"
        )
        expires_in: int = int(auth_resp.get("expires_in", _DEVICE_EXPIRES_IN_DEFAULT))
        server_interval: float = float(
            auth_resp.get("interval", _DEVICE_POLL_INTERVAL_DEFAULT)
        )
        poll_interval: float = (
            poll_interval_override
            if poll_interval_override is not None
            else server_interval
        )
        deadline_seconds: float = (
            deadline_override if deadline_override is not None else float(expires_in)
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # ── Step 2: notify user ──────────────────────────────────────────
        # ``expires_in`` is surfaced so callers can show a deadline to the
        # user (= avoids the "device code expired with no warning" UX
        # failure mode). Existing consumers that only read ``user_code`` /
        # ``verification_uri`` keep working — the new keys are additive.
        user_action_data: dict[str, Any] = {
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
        }
        if verification_uri_complete:
            user_action_data["verification_uri_complete"] = verification_uri_complete

        if on_user_action is not None:
            on_user_action(user_action_data)
        else:
            print(f"Visit: {verification_uri}")
            print(f"Enter code: {user_code}")
            if verification_uri_complete:
                print(f"Or open: {verification_uri_complete}")

        # ── Step 3: emit oauth_login_started (P6) ───────────────────────
        # Log only the last 4 chars of device_code for security.
        device_code_tail = device_code[-4:] if len(device_code) >= 4 else device_code
        if events is not None:
            try:
                events.emit(
                    "oauth_login_started",
                    key=provider.name,
                    provider=provider.name,
                    device_code=device_code_tail,
                    verification_uri=verification_uri,
                    expires_at=expires_at.isoformat(),
                )
            except Exception:  # noqa: BLE001 — emit failure must not break flow
                pass

        # ── Step 4: poll loop (wrapped in deadline) ──────────────────────
        async def _poll_loop() -> OAuthToken:
            nonlocal poll_interval
            while True:
                await asyncio.sleep(poll_interval)
                status, body = await _poll_token(provider, device_code, http_client)

                if status == 200 and body.get("access_token"):
                    # SUCCESS path.
                    return _parse_token_success(body, provider=provider)

                error_code: str = body.get("error", "")

                if status == 400 and error_code == "authorization_pending":
                    # Normal — user has not yet approved; keep waiting.
                    continue
                elif status == 400 and error_code == "slow_down":
                    # Server instructs us to back off.
                    poll_interval += _SLOW_DOWN_INCREMENT
                    continue
                elif status == 400 and error_code == "access_denied":
                    raise DeviceGrantError(
                        f"User denied the {provider.name!r} authorization request.",
                        error_code="access_denied",
                    )
                elif status == 400 and error_code == "expired_token":
                    raise DeviceGrantError(
                        f"Device code for {provider.name!r} has expired.",
                        error_code="expired_token",
                    )
                else:
                    # Unexpected status or error — surface verbatim.
                    detail = error_code or f"HTTP {status}"
                    raise DeviceGrantError(
                        f"Unexpected polling response from {provider.name!r}: "
                        f"{detail}.",
                        error_code=error_code or None,
                    )

        # ``asyncio.timeout()`` instead of ``asyncio.wait_for`` because the
        # latter can wrap the awaited coroutine in a new asyncio.Task and
        # ``_poll_loop`` issues HTTP requests via ``httpx.AsyncClient``,
        # which opens anyio cancel scopes internally. On timeout
        # cancellation, the cleanup would run in a different task than
        # the entry → ``RuntimeError: Attempted to exit cancel scope in
        # a different task...``. ``asyncio.timeout()`` is a task-local
        # deadline (= no task wrap) and httpx unwinds cleanly.
        try:
            async with asyncio.timeout(deadline_seconds):
                token = await _poll_loop()
        except asyncio.TimeoutError:
            raise DeviceGrantError(
                f"Device grant for {provider.name!r} timed out after "
                f"{deadline_seconds:.0f}s.",
                error_code="timeout",
            )
        # CancelledError propagates as-is (= abort-capable per spec).

        # ── Step 5: emit oauth_login_completed (P6) ──────────────────────
        if events is not None:
            try:
                events.emit(
                    "oauth_login_completed",
                    key=provider.name,
                    expires_at=token.expires_at.isoformat(),
                    scopes=list(token.scopes),
                )
            except Exception:  # noqa: BLE001 — emit failure must not break flow
                pass

        return token

    finally:
        if owns_client:
            await http_client.aclose()
