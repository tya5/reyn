"""Reyn universal secret handling (ADR-0030).

Public API:
  - ``load_secrets_to_environ()``  — startup dotenv loader
  - ``save_secret(key, value)``    — programmatic write
  - ``load_secrets() -> dict``     — read all secrets
  - ``clear_secret(key)``          — remove one secret
  - ``list_secret_keys() -> list`` — list KEY names only
  - ``expand_env(value)``          — generic ${VAR} resolver
"""
from __future__ import annotations

from .interpolation import expand_env
from .loader import load_secrets_to_environ
from .store import clear_secret, list_secret_keys, load_secrets, save_secret

__all__ = [
    "expand_env",
    "load_secrets_to_environ",
    "save_secret",
    "load_secrets",
    "clear_secret",
    "list_secret_keys",
]
