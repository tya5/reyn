"""Tier 2: `reyn auth login` device-grant UX P3 (issue #291 Priority 3).

Pins:

  1. ``_print_scope_advertisement`` prints the configured scopes before
     the device_authorization POST (= user sees what permissions reyn
     is about to ask for, can abort before any network round-trip).
  2. ``_classify_token_status`` returns ``expired`` / ``near-expiry``
     / ``valid`` matching the OAuth refresh buffer threshold (60 s).
  3. ``run_list`` surfaces the 3-state classification end-to-end with
     fixture tokens written to a tmp_path OAuth store.

P3 #8 (re-auth surface elevation) is deferred — see PR #298 body for
the documented dependency on ``get_valid_token`` consumer wire-up.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.cli.commands import auth as auth_mod

# ── _print_scope_advertisement ──────────────────────────────────────────────


class _ProviderStub:
    """Bare-minimum stand-in for OAuthProviderConfig — only attributes
    ``_print_scope_advertisement`` reads. Avoids importing the heavy
    config module in this unit test."""

    def __init__(self, name: str, scopes: list[str]) -> None:
        self.name = name
        self.scopes = scopes


def test_scope_advertisement_lists_configured_scopes(
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: provider with scopes → comma-joined list printed to stderr."""
    provider = _ProviderStub("github", ["repo", "read:user"])
    auth_mod._print_scope_advertisement(provider)

    err = capsys.readouterr().err
    assert "Requesting scopes for 'github'" in err
    assert "repo" in err
    assert "read:user" in err


def test_scope_advertisement_skips_when_no_scopes(
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: empty scopes list → no output (= provider does not
    request anything, advertising would be noise)."""
    provider = _ProviderStub("public_api", [])
    auth_mod._print_scope_advertisement(provider)

    assert capsys.readouterr().err == ""


def test_scope_advertisement_handles_missing_scopes_attr(
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: defensive — ``provider.scopes`` missing → no crash, no print."""

    class _BareProvider:
        name = "minimal"

    auth_mod._print_scope_advertisement(_BareProvider())
    assert capsys.readouterr().err == ""


# ── _classify_token_status ──────────────────────────────────────────────────


def test_classify_token_status_expired() -> None:
    """Tier 2: negative delta → ``expired`` (= past expires_at)."""
    assert auth_mod._classify_token_status(-1.0) == "expired"
    assert auth_mod._classify_token_status(-3600.0) == "expired"


def test_classify_token_status_near_expiry() -> None:
    """Tier 2: delta in [0, 60) → ``near-expiry`` (= refresh window).

    The 60 s threshold matches ``oauth.py:_REFRESH_BUFFER_SECONDS`` — a
    token within 60 s of expiry will refresh on next ``get_valid_token``
    call, so flagging it explicitly helps users predict behavior.
    """
    assert auth_mod._classify_token_status(0.0) == "near-expiry"
    assert auth_mod._classify_token_status(30.0) == "near-expiry"
    assert auth_mod._classify_token_status(59.999) == "near-expiry"


def test_classify_token_status_valid() -> None:
    """Tier 2: delta >= 60 → ``valid`` (= no refresh needed)."""
    assert auth_mod._classify_token_status(60.0) == "valid"
    assert auth_mod._classify_token_status(3600.0) == "valid"
    assert auth_mod._classify_token_status(86400.0) == "valid"


# ── run_list end-to-end with 3-state fixtures ───────────────────────────────


def _write_token_fixture(
    store_path: Path, key: str, expires_at: datetime,
) -> None:
    """Write a minimal OAuthToken JSON to *store_path* under *key*."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if store_path.exists():
        existing = json.loads(store_path.read_text())
    existing[key] = {
        "access_token": "AT_x",
        "refresh_token": "RT_x",
        "token_uri": "https://example.com/token",
        "client_id": "cid",
        "expires_at": expires_at.isoformat(),
        "scopes": ["repo"],
        "client_secret": None,
    }
    store_path.write_text(json.dumps(existing, sort_keys=True))
    store_path.chmod(0o600)


def test_run_list_surfaces_all_three_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: three fixture tokens (expired / near-expiry / valid) →
    each labelled with the correct status in the output.

    Issue #291 P3 #9: previously the 2-state label collapsed
    ``expired`` into ``near-expiry``, so an oncall user reading the
    list could not tell whether the next API call would refresh
    (= recoverable) or fail with re-auth (= needs human action)."""
    store = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(store))

    now = datetime.now().astimezone()
    _write_token_fixture(store, "expired_gh", now - timedelta(hours=1))
    _write_token_fixture(store, "near_gh", now + timedelta(seconds=30))
    _write_token_fixture(store, "valid_gh", now + timedelta(hours=1))

    args = argparse.Namespace()
    auth_mod.run_list(args)

    out = capsys.readouterr().out
    # Each fixture key appears with its corresponding state.
    assert "expired_gh: expired" in out
    assert "near_gh: near-expiry" in out
    assert "valid_gh: valid" in out


def test_run_list_expired_is_distinct_from_near_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: regression guard — ``expired`` must NOT be labelled
    ``near-expiry`` (= the pre-P3 2-state behavior would conflate them,
    masking the recoverability distinction)."""
    store = tmp_path / "oauth_tokens.json"
    monkeypatch.setenv("REYN_OAUTH_TOKENS_PATH", str(store))

    now = datetime.now().astimezone()
    _write_token_fixture(store, "stale", now - timedelta(days=1))

    auth_mod.run_list(argparse.Namespace())

    out = capsys.readouterr().out
    assert "stale: expired" in out
    assert "stale: near-expiry" not in out


# Notes on timezone-naive expires_at: ``OAuthToken.from_dict`` accepts the
# stored ISO string verbatim. The fixtures above use timezone-aware
# `now.astimezone()` so the `expires_at - now` arithmetic in `run_list`
# (which converts via `now = datetime.now().astimezone()`) matches.
