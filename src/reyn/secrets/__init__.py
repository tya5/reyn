"""Reyn universal secret handling (ADR-0030 + FP-0016 Component B + C).

Public API:
  - ``load_secrets_to_environ()``  — startup dotenv loader
  - ``save_secret(key, value)``    — programmatic write
  - ``load_secrets() -> dict``     — read all secrets
  - ``clear_secret(key)``          — remove one secret
  - ``list_secret_keys() -> list`` — list KEY names only
  - ``expand_env(value)``          — generic ${VAR} resolver

OAuth (FP-0016 Component B):
  - ``OAuthToken``                 — value type for refresh-capable tokens
  - ``OAuthRefreshError``          — raised on refresh failure
  - ``get_valid_token(key)``       — async; refresh if within 60 s of expiry
  - ``save_oauth_token(key, tok)`` — persist a token (used by Component C
    ``reyn auth login`` when it lands)
  - ``load_oauth_token(key)``      — read a single token by key
  - ``list_oauth_token_keys()``    — list keys present in the store
  - ``clear_oauth_token(key)``     — remove a token from the store

OAuth (FP-0016 Component C — RFC 8628 Device Authorization Grant):
  - ``OAuthProviderConfig``        — provider config dataclass (reyn.yaml)
  - ``DeviceGrantError``           — raised on access_denied / timeout / etc.
  - ``device_grant_flow(provider)``— async; runs full RFC 8628 flow,
    returns an OAuthToken ready for ``save_oauth_token``
"""
from __future__ import annotations

from .interpolation import expand_env
from .loader import load_secrets_to_environ
from .oauth import (
    DeviceGrantError,
    OAuthProviderConfig,
    OAuthRefreshError,
    OAuthToken,
    clear_oauth_token,
    device_grant_flow,
    get_valid_token,
    list_oauth_token_keys,
    load_oauth_token,
    save_oauth_token,
)
from .store import clear_secret, list_secret_keys, load_secrets, save_secret

__all__ = [
    "expand_env",
    "load_secrets_to_environ",
    "save_secret",
    "load_secrets",
    "clear_secret",
    "list_secret_keys",
    "OAuthToken",
    "OAuthRefreshError",
    "get_valid_token",
    "save_oauth_token",
    "load_oauth_token",
    "list_oauth_token_keys",
    "clear_oauth_token",
    "OAuthProviderConfig",
    "DeviceGrantError",
    "device_grant_flow",
]
