"""Tier 1 + Tier 2: FP-0016 Component B — OAuth refresh lifecycle.

Covers:
- OAuthToken serialization round-trip (Tier 1 contract)
- is_expired() boundary behaviour
- save_oauth_token / load_oauth_token / list / clear
- get_valid_token: cached return when fresh / refresh when near expiry
- P6 events: token_refreshed on success / token_refresh_failed on error
- Concurrency: per-key lock serialises concurrent get_valid_token calls

No MagicMock / AsyncMock; httpx.MockTransport (built into httpx) is a
real httpx transport that returns canned responses — that's a Fake by
the testing-policy taxonomy (real instance, just one collaborator pinned).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from reyn.core.events.events import EventLog
from reyn.security.secrets.oauth import (
    _REFRESH_LOCKS,
    OAuthRefreshError,
    OAuthToken,
    clear_oauth_token,
    get_valid_token,
    list_oauth_token_keys,
    load_oauth_token,
    save_oauth_token,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _make_token(*, expires_in_seconds: int = 3600, **overrides) -> OAuthToken:
    """Construct a representative OAuthToken with adjustable expiry."""
    defaults = {
        "access_token": "AT_old",
        "refresh_token": "RT_old",
        "token_uri": "https://example.com/token",
        "client_id": "cid_abc",
        "client_secret": "csec_xyz",
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        "scopes": ["read", "write"],
    }
    defaults.update(overrides)
    return OAuthToken(**defaults)


def _mock_transport(handler) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with a MockTransport that calls *handler*.

    ``handler(request) -> httpx.Response`` lets each test pin its own
    response shape without a global side-effect.
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def _clear_locks() -> None:
    """Each test starts with a clean per-key lock dict to avoid bleed-over."""
    _REFRESH_LOCKS.clear()


@pytest.fixture
def oauth_store_path(tmp_path, monkeypatch) -> Path:
    """Per-test OAuth token store at tmp_path."""
    p = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(p))
    return p


# ── 1. OAuthToken serialization round-trip ─────────────────────────────────


def test_oauth_token_to_dict_round_trip() -> None:
    """Tier 1: OAuthToken.to_dict / from_dict preserves all fields."""
    original = _make_token()
    serialized = original.to_dict()
    # expires_at must be ISO 8601 string after to_dict
    assert isinstance(serialized["expires_at"], str)
    restored = OAuthToken.from_dict(serialized)
    assert restored.access_token == original.access_token
    assert restored.refresh_token == original.refresh_token
    assert restored.token_uri == original.token_uri
    assert restored.client_id == original.client_id
    assert restored.client_secret == original.client_secret
    assert restored.expires_at == original.expires_at
    assert restored.scopes == original.scopes


def test_oauth_token_from_dict_rejects_bad_expires() -> None:
    """Tier 1: from_dict rejects non-ISO non-datetime expires_at."""
    with pytest.raises(ValueError, match="expires_at must be ISO"):
        OAuthToken.from_dict({
            "access_token": "x", "refresh_token": "y", "token_uri": "u",
            "client_id": "c", "expires_at": 12345,
        })


def test_oauth_token_public_client_no_secret() -> None:
    """Tier 1: client_secret is optional for public OAuth clients."""
    token = _make_token(client_secret=None)
    assert token.client_secret is None
    round_trip = OAuthToken.from_dict(token.to_dict())
    assert round_trip.client_secret is None


# ── 2. is_expired boundary ─────────────────────────────────────────────────


def test_is_expired_true_when_inside_buffer() -> None:
    """Tier 1: within 60 s of expiry → is_expired() True (refresh-due)."""
    token = _make_token(expires_in_seconds=30)
    assert token.is_expired() is True


def test_is_expired_false_when_outside_buffer() -> None:
    """Tier 1: >60 s remaining → is_expired() False (cached return)."""
    token = _make_token(expires_in_seconds=600)
    assert token.is_expired() is False


def test_is_expired_naive_timestamp_treated_as_expired() -> None:
    """Tier 1: naive (no tz) expires_at is conservatively expired."""
    # Bypass the aware-only contract via __dict__ to simulate corrupt data.
    token = _make_token()
    object.__setattr__(token, "expires_at", datetime.now())  # naive
    assert token.is_expired() is True


# ── 3. Store CRUD ──────────────────────────────────────────────────────────


def test_save_then_load_round_trip(oauth_store_path: Path) -> None:
    """Tier 2: save_oauth_token + load_oauth_token via env-override path."""
    token = _make_token()
    save_oauth_token("github", token)
    loaded = load_oauth_token("github")
    assert loaded is not None
    assert loaded.access_token == token.access_token
    assert loaded.scopes == token.scopes


def test_load_missing_key_returns_none(oauth_store_path: Path) -> None:
    """Tier 2: load_oauth_token on unknown key → None (not exception)."""
    assert load_oauth_token("unknown") is None


def test_list_keys_then_clear(oauth_store_path: Path) -> None:
    """Tier 2: list + clear round-trip."""
    save_oauth_token("a", _make_token())
    save_oauth_token("b", _make_token())
    keys = list_oauth_token_keys()
    assert set(keys) == {"a", "b"}
    assert clear_oauth_token("a") is True
    assert clear_oauth_token("a") is False  # idempotent
    assert list_oauth_token_keys() == ["b"]


def test_save_empty_key_rejected(oauth_store_path: Path) -> None:
    """Tier 1: empty key → ValueError (don't allow nameless tokens)."""
    with pytest.raises(ValueError, match="must not be empty"):
        save_oauth_token("", _make_token())


def test_store_file_chmod_600(oauth_store_path: Path) -> None:
    """Tier 2: write enforces chmod 600 (= secrets policy)."""
    save_oauth_token("k", _make_token())
    mode = oauth_store_path.stat().st_mode & 0o777
    assert mode == 0o600


# ── 4. get_valid_token cache path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_valid_token_returns_cached_when_fresh(oauth_store_path: Path) -> None:
    """Tier 2: token with >60 s left → no refresh, cached access_token returned."""
    save_oauth_token("github", _make_token(expires_in_seconds=600))
    called = {"count": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={"access_token": "AT_new", "expires_in": 3600})

    client = _mock_transport(_handler)
    try:
        result = await get_valid_token("github", http_client=client)
    finally:
        await client.aclose()
    assert result == "AT_old"
    assert called["count"] == 0  # no HTTP call


@pytest.mark.asyncio
async def test_get_valid_token_missing_key_raises(oauth_store_path: Path) -> None:
    """Tier 2: get_valid_token on unknown key → KeyError."""
    with pytest.raises(KeyError, match="unknown"):
        await get_valid_token("unknown")


# ── 5. get_valid_token refresh path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_valid_token_refreshes_when_expired(oauth_store_path: Path) -> None:
    """Tier 2: token within 60 s of expiry → refresh issued, new token saved."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    request_seen = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        # Capture for assertion; httpx.Request.content is bytes.
        from urllib.parse import parse_qs
        request_seen["body"] = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "AT_new",
                "refresh_token": "RT_new",
                "expires_in": 3600,
                "scope": "read write admin",
            },
        )

    events = EventLog()
    client = _mock_transport(_handler)
    try:
        result = await get_valid_token("github", events=events, http_client=client)
    finally:
        await client.aclose()
    assert result == "AT_new"

    # Persisted with new fields
    saved = load_oauth_token("github")
    assert saved.access_token == "AT_new"
    assert saved.refresh_token == "RT_new"
    assert saved.scopes == ["read", "write", "admin"]
    assert not saved.is_expired()

    # P6 token_refreshed event emitted
    emitted = [e for e in events.all() if e.type == "token_refreshed"]
    assert emitted, "expected at least one token_refreshed event"
    assert emitted[0].data["key"] == "github"
    assert "expires_at" in emitted[0].data

    # Request shape: grant_type + refresh_token + client_id + client_secret
    body = request_seen["body"]
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["RT_old"]
    assert body["client_id"] == ["cid_abc"]
    assert body["client_secret"] == ["csec_xyz"]


@pytest.mark.asyncio
async def test_refresh_response_without_refresh_token_keeps_old(oauth_store_path: Path) -> None:
    """Tier 2: server omits refresh_token → reuse the old one (RFC 6749 §6)."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "AT_new", "expires_in": 3600},
        )

    client = _mock_transport(_handler)
    try:
        await get_valid_token("github", http_client=client)
    finally:
        await client.aclose()
    saved = load_oauth_token("github")
    assert saved.refresh_token == "RT_old"


@pytest.mark.asyncio
async def test_refresh_missing_access_token_raises(oauth_store_path: Path) -> None:
    """Tier 2: 200 OK without access_token → OAuthRefreshError + event."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    events = EventLog()
    client = _mock_transport(_handler)
    try:
        with pytest.raises(OAuthRefreshError, match="missing 'access_token'"):
            await get_valid_token("github", events=events, http_client=client)
    finally:
        await client.aclose()

    failures = [e for e in events.all() if e.type == "token_refresh_failed"]
    assert failures, "expected at least one token_refresh_failed event"
    assert failures[0].data["key"] == "github"
    assert failures[0].data["re_auth_required"] is False


# ── 6. get_valid_token error paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_400_marks_re_auth_required(oauth_store_path: Path) -> None:
    """Tier 1: 400 invalid_grant → OAuthRefreshError(re_auth_required=True)."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    events = EventLog()
    client = _mock_transport(_handler)
    try:
        with pytest.raises(OAuthRefreshError) as exc_info:
            await get_valid_token("github", events=events, http_client=client)
    finally:
        await client.aclose()
    assert exc_info.value.re_auth_required is True
    assert exc_info.value.status_code == 400

    failures = [e for e in events.all() if e.type == "token_refresh_failed"]
    assert failures[0].data["re_auth_required"] is True


@pytest.mark.asyncio
async def test_refresh_500_transient_no_re_auth(oauth_store_path: Path) -> None:
    """Tier 1: 500 server error → re_auth_required=False (caller may retry)."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    client = _mock_transport(_handler)
    try:
        with pytest.raises(OAuthRefreshError) as exc_info:
            await get_valid_token("github", http_client=client)
    finally:
        await client.aclose()
    assert exc_info.value.re_auth_required is False
    assert exc_info.value.status_code == 500


# ── 7. Concurrency: per-key lock serialises ───────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_calls_serialise_to_one_refresh(oauth_store_path: Path) -> None:
    """Tier 2: 5 concurrent get_valid_token for same key → 1 HTTP refresh."""
    save_oauth_token("github", _make_token(expires_in_seconds=10))

    refresh_count = {"n": 0}

    async def _slow_handler(request: httpx.Request) -> httpx.Response:
        # Each request increments + yields so concurrent waiters
        # accumulate behind the lock if locking works.
        refresh_count["n"] += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": f"AT_v{refresh_count['n']}",
                "expires_in": 3600,
            },
        )

    client = _mock_transport(_slow_handler)
    try:
        tokens = await asyncio.gather(*[
            get_valid_token("github", http_client=client) for _ in range(5)
        ])
    finally:
        await client.aclose()
    # All 5 callers receive the same access_token = exactly one HTTP refresh.
    assert refresh_count["n"] == 1
    assert all(t == "AT_v1" for t in tokens)


# ── 8. Store-file resilience ───────────────────────────────────────────────


def test_load_handles_malformed_json(oauth_store_path: Path) -> None:
    """Tier 2: corrupt JSON → warning + empty result (not crash)."""
    oauth_store_path.parent.mkdir(parents=True, exist_ok=True)
    oauth_store_path.write_text("{ not json")
    with pytest.warns(UserWarning, match="not valid JSON"):
        result = load_oauth_token("anything")
    assert result is None


def test_load_handles_object_with_missing_fields(oauth_store_path: Path) -> None:
    """Tier 2: malformed token entry → warning + None for that key."""
    oauth_store_path.parent.mkdir(parents=True, exist_ok=True)
    oauth_store_path.write_text(json.dumps({"github": {"access_token": "x"}}))
    with pytest.warns(UserWarning, match="malformed"):
        result = load_oauth_token("github")
    assert result is None
