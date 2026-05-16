"""Tier 2: FP-0016 Component C — AuthConfig parser contract.

Tests reyn.yaml auth.providers parsing:
- None / empty → empty providers dict
- valid full spec → OAuthProviderConfig populated
- missing required field → ValueError
- invalid types → ValueError
- unknown fields → silently ignored (forward compat)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    AuthConfig,
    ReynConfig,
    _build_auth_config,
    load_config,
)

# ── 1. Default values ─────────────────────────────────────────────────────────


def test_default_is_empty() -> None:
    """Tier 2: AuthConfig() has empty providers dict by default."""
    cfg = AuthConfig()
    assert isinstance(cfg.providers, dict)
    assert len(cfg.providers) == 0


def test_reyn_config_carries_auth_default() -> None:
    """Tier 2: ReynConfig default-constructs with an AuthConfig."""
    cfg = ReynConfig()
    assert isinstance(cfg.auth, AuthConfig)
    assert isinstance(cfg.auth.providers, dict)


# ── 2. Parser — happy path ────────────────────────────────────────────────────


def test_parser_none_returns_empty() -> None:
    """Tier 2: _build_auth_config(None) → empty AuthConfig."""
    cfg = _build_auth_config(None)
    assert isinstance(cfg, AuthConfig)
    assert cfg.providers == {}


def test_parser_full_spec_round_trip() -> None:
    """Tier 2: full provider spec → OAuthProviderConfig fields all populated."""
    from reyn.secrets.oauth import OAuthProviderConfig

    raw = {
        "providers": {
            "github": {
                "client_id": "cid_abc",
                "device_authorization_url": "https://github.com/login/device/code",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["repo", "user:email"],
                "client_secret": "csec_xyz",
                "audience": "https://api.github.com",
            }
        }
    }
    cfg = _build_auth_config(raw)
    assert "github" in cfg.providers
    p = cfg.providers["github"]
    assert isinstance(p, OAuthProviderConfig)
    assert p.name == "github"
    assert p.client_id == "cid_abc"
    assert p.device_authorization_url == "https://github.com/login/device/code"
    assert p.token_url == "https://github.com/login/oauth/access_token"
    assert p.scopes == ["repo", "user:email"]
    assert p.client_secret == "csec_xyz"
    assert p.audience == "https://api.github.com"


def test_parser_multiple_providers() -> None:
    """Tier 2: two providers in same auth block → both parsed independently."""
    raw = {
        "providers": {
            "github": {
                "client_id": "cid_gh",
                "device_authorization_url": "https://github.com/login/device/code",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["repo"],
            },
            "google": {
                "client_id": "cid_google",
                "device_authorization_url": "https://oauth2.googleapis.com/device/code",
                "token_url": "https://oauth2.googleapis.com/token",
                "scopes": ["openid", "email"],
            },
        }
    }
    cfg = _build_auth_config(raw)
    assert set(cfg.providers.keys()) == {"github", "google"}
    assert cfg.providers["github"].client_id == "cid_gh"
    assert cfg.providers["google"].client_id == "cid_google"


def test_parser_omits_optional_secret_and_audience() -> None:
    """Tier 2: client_secret and audience are None when omitted (public client)."""
    raw = {
        "providers": {
            "acme": {
                "client_id": "cid_acme",
                "device_authorization_url": "https://acme.example.com/device",
                "token_url": "https://acme.example.com/token",
                "scopes": [],
            }
        }
    }
    cfg = _build_auth_config(raw)
    p = cfg.providers["acme"]
    assert p.client_secret is None
    assert p.audience is None


def test_parser_ignores_unknown_keys() -> None:
    """Tier 2: unknown provider fields are silently ignored (forward compat)."""
    raw = {
        "providers": {
            "future": {
                "client_id": "cid_future",
                "device_authorization_url": "https://future.example.com/device",
                "token_url": "https://future.example.com/token",
                "scopes": ["read"],
                "future_field_v2": "ignored",
                "another_unknown": 42,
            }
        }
    }
    cfg = _build_auth_config(raw)
    assert "future" in cfg.providers
    assert cfg.providers["future"].client_id == "cid_future"


# ── 3. Parser — validation errors ─────────────────────────────────────────────


def test_parser_rejects_non_mapping() -> None:
    """Tier 2: non-mapping at top level raises ValueError with 'must be a mapping'."""
    with pytest.raises(ValueError, match="must be a mapping"):
        _build_auth_config("not a dict")


def test_parser_rejects_missing_client_id() -> None:
    """Tier 2: missing client_id raises ValueError mentioning 'client_id'."""
    raw = {
        "providers": {
            "bad": {
                "device_authorization_url": "https://example.com/device",
                "token_url": "https://example.com/token",
            }
        }
    }
    with pytest.raises(ValueError, match="client_id"):
        _build_auth_config(raw)


def test_parser_rejects_missing_device_authorization_url() -> None:
    """Tier 2: missing device_authorization_url raises ValueError."""
    raw = {
        "providers": {
            "bad": {
                "client_id": "cid",
                "token_url": "https://example.com/token",
            }
        }
    }
    with pytest.raises(ValueError, match="device_authorization_url"):
        _build_auth_config(raw)


def test_parser_rejects_missing_token_url() -> None:
    """Tier 2: missing token_url raises ValueError."""
    raw = {
        "providers": {
            "bad": {
                "client_id": "cid",
                "device_authorization_url": "https://example.com/device",
            }
        }
    }
    with pytest.raises(ValueError, match="token_url"):
        _build_auth_config(raw)


def test_parser_rejects_non_list_scopes() -> None:
    """Tier 2: scopes with non-list value raises ValueError."""
    raw = {
        "providers": {
            "bad": {
                "client_id": "cid",
                "device_authorization_url": "https://example.com/device",
                "token_url": "https://example.com/token",
                "scopes": "repo user:email",  # string instead of list
            }
        }
    }
    with pytest.raises(ValueError, match="scopes"):
        _build_auth_config(raw)


def test_parser_rejects_non_string_client_secret() -> None:
    """Tier 2: client_secret with non-string raises ValueError."""
    raw = {
        "providers": {
            "bad": {
                "client_id": "cid",
                "device_authorization_url": "https://example.com/device",
                "token_url": "https://example.com/token",
                "client_secret": 12345,  # int instead of string
            }
        }
    }
    with pytest.raises(ValueError, match="client_secret"):
        _build_auth_config(raw)


def test_parser_empty_provider_name_rejected() -> None:
    """Tier 2: empty-string provider name raises ValueError."""
    raw = {
        "providers": {
            "": {
                "client_id": "cid",
                "device_authorization_url": "https://example.com/device",
                "token_url": "https://example.com/token",
            }
        }
    }
    with pytest.raises(ValueError):
        _build_auth_config(raw)


# ── 4. End-to-end load_config integration ─────────────────────────────────────


def test_load_config_integration_via_yaml(tmp_path: Path) -> None:
    """Tier 2: load_config reads auth.providers from reyn.yaml round-trip."""
    (tmp_path / "reyn.yaml").write_text(
        """
auth:
  providers:
    github:
      client_id: "ghcid_test"
      device_authorization_url: "https://github.com/login/device/code"
      token_url: "https://github.com/login/oauth/access_token"
      scopes:
        - repo
        - user:email
      client_secret: "ghsec_test"
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert isinstance(cfg.auth, AuthConfig)
    assert "github" in cfg.auth.providers
    p = cfg.auth.providers["github"]
    assert p.client_id == "ghcid_test"
    assert p.scopes == ["repo", "user:email"]
    assert p.client_secret == "ghsec_test"


def test_load_config_without_auth_block_uses_defaults(tmp_path: Path) -> None:
    """Tier 2: omitting auth: block in reyn.yaml → empty AuthConfig."""
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert isinstance(cfg.auth, AuthConfig)
    assert cfg.auth.providers == {}
