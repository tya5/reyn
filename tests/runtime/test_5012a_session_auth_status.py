"""Tier 1: `session_auth_status.py` — reyn-managed OAuth credential status
for `describe_session` (#5012-A).

Real OAuth token store (`save_oauth_token`/`load_oauth_token` against an
isolated `tmp_path` file, no mocks) — the same real-instance pattern
`test_fp0016_b_oauth_refresh.py` uses for this exact subsystem.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reyn.runtime.session_auth_status import describe_auth_status
from reyn.security.secrets.oauth import OAuthToken, save_oauth_token


@dataclass
class _FakeAuthConfig:
    providers: Any = None


def _make_token(*, expires_in_seconds: int) -> OAuthToken:
    return OAuthToken(
        access_token="AT",
        refresh_token="RT",
        token_uri="https://example.com/token",
        client_id="cid",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )


def test_no_providers_declared_reports_empty():
    """Tier 1: `auth.providers` absent/empty — nothing to report, not an
    error."""
    result = describe_auth_status(_FakeAuthConfig(providers={}))
    assert result == {}


def test_declared_provider_with_no_stored_token_is_not_authenticated(
    tmp_path: Path,
) -> None:
    """Tier 1: a provider IS declared under `auth.providers` but has never
    had `reyn auth login` run for it — authenticated=False, with a reason
    naming the remedy (not just a bare False)."""
    store = tmp_path / "oauth_tokens.json"
    result = describe_auth_status(
        _FakeAuthConfig(providers={"github": object()}), token_store_path=store,
    )
    assert result["github"]["authenticated"] is False
    assert "reyn auth login" in result["github"]["reason"]


def test_declared_provider_with_a_valid_token_is_authenticated(tmp_path: Path) -> None:
    """Tier 1: a real, unexpired token in the store — authenticated=True."""
    store = tmp_path / "oauth_tokens.json"
    save_oauth_token("github", _make_token(expires_in_seconds=3600), path=store)

    result = describe_auth_status(
        _FakeAuthConfig(providers={"github": object()}), token_store_path=store,
    )

    assert result == {"github": {"authenticated": True, "reason": "token present and valid"}}


def test_declared_provider_with_an_expired_token_is_not_authenticated(
    tmp_path: Path,
) -> None:
    """Tier 1: a stored token past its expiry (buffer) — authenticated=False,
    reason distinguishes this from "never logged in" (auto-refresh is
    expected, not a login prompt)."""
    store = tmp_path / "oauth_tokens.json"
    save_oauth_token("github", _make_token(expires_in_seconds=-10), path=store)

    result = describe_auth_status(
        _FakeAuthConfig(providers={"github": object()}), token_store_path=store,
    )

    assert result["github"]["authenticated"] is False
    assert "expired" in result["github"]["reason"]


def test_no_token_or_scope_ever_appears_in_the_result(tmp_path: Path) -> None:
    """Tier 1: architect constraint (#5012-A) — the result never carries the
    access token, refresh token, client secret, or scopes, regardless of
    which state a provider is in. Checked structurally (only the 2 declared
    keys exist per provider), not just by absence of a specific string."""
    store = tmp_path / "oauth_tokens.json"
    token = _make_token(expires_in_seconds=3600)
    save_oauth_token("github", token, path=store)

    result = describe_auth_status(
        _FakeAuthConfig(providers={"github": object()}), token_store_path=store,
    )

    assert set(result["github"].keys()) == {"authenticated", "reason"}
    assert token.access_token not in str(result)
    assert token.refresh_token not in str(result)


def test_multiple_declared_providers_are_reported_independently(tmp_path: Path) -> None:
    """Tier 1: one authenticated, one not — each provider's own state, not
    a single flattened verdict."""
    store = tmp_path / "oauth_tokens.json"
    save_oauth_token("github", _make_token(expires_in_seconds=3600), path=store)

    result = describe_auth_status(
        _FakeAuthConfig(providers={"github": object(), "google": object()}),
        token_store_path=store,
    )

    assert result["github"]["authenticated"] is True
    assert result["google"]["authenticated"] is False
