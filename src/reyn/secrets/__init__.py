"""Reyn universal secret handling (ADR-0030 + FP-0016 Component B).

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
"""
from __future__ import annotations

from .interpolation import expand_env
from .loader import load_secrets_to_environ
from .oauth import (
    OAuthRefreshError,
    OAuthToken,
    clear_oauth_token,
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
]
