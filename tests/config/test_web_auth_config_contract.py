"""Tier 1: the ``gateway.auth`` reyn.yaml schema (ADR-0039 P0 auth config surface).

Operators configure the gateway auth model in reyn.yaml. This pins the config
contract: the ``gateway.auth`` keys parse to typed fields, non-default values
round-trip, and a missing section yields secure defaults (token unset,
loopback token required).

#4174 T4: renamed from ``web.auth`` — ``web:`` conflated the web_fetch TOOL's
TLS settings with the ``reyn web`` GATEWAY's own settings under one key;
this is the gateway half, now under its own ``gateway:`` top-level key.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _load(tmp_path: Path, yaml: str, monkeypatch):
    (tmp_path / "reyn.yaml").write_text(yaml, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    os.environ.pop("REYN_WEB_AUTH_TOKEN", None)
    from reyn.config import load_config

    return load_config()


def test_gateway_auth_non_default_values_round_trip(tmp_path, monkeypatch):
    """Tier 1: every gateway.auth key parses to its typed field with the given value."""
    cfg = _load(
        tmp_path,
        (
            "model: standard\n"
            "gateway:\n"
            "  auth:\n"
            "    token: my-secret-token\n"
            "    require_token_on_loopback: false\n"
            "    tls_certfile: /etc/certs/server.pem\n"
            "    tls_keyfile: /etc/certs/server.key\n"
        ),
        monkeypatch,
    )
    auth = cfg.gateway.auth
    assert auth.token == "my-secret-token"
    assert auth.require_token_on_loopback is False
    assert auth.tls_certfile == "/etc/certs/server.pem"
    assert auth.tls_keyfile == "/etc/certs/server.key"


def test_gateway_auth_defaults_are_secure(tmp_path, monkeypatch):
    """Tier 1: a missing gateway.auth section defaults to no token + loopback token required."""
    cfg = _load(tmp_path, "model: standard\n", monkeypatch)
    auth = cfg.gateway.auth
    assert auth.token is None
    assert auth.require_token_on_loopback is True
    assert auth.tls_certfile is None
    assert auth.tls_keyfile is None


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_require_token_on_loopback_truthy_strings(tmp_path, monkeypatch, value):
    """Tier 1: an interpolated truthy string for require_token_on_loopback parses True."""
    cfg = _load(
        tmp_path,
        f"model: standard\ngateway:\n  auth:\n    require_token_on_loopback: '{value}'\n",
        monkeypatch,
    )
    assert cfg.gateway.auth.require_token_on_loopback is True
