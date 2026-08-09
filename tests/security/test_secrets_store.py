"""Tier 2: OS invariant — secrets store CRUD API.

Pins the contract for the public API in ``reyn.security.secrets.store``:

  - save_secret: creates the file, sets chmod 600, stores KEY=value
  - save_secret: updates an existing key in-place (other keys preserved)
  - load_secrets: returns {key: value} dict; absent file → empty dict
  - clear_secret: removes a key; returns True if found, False if not
  - clear_secret: idempotent — clearing a missing key returns False (no error)
  - list_secret_keys: returns key names without values; preserves order
  - Roundtrip: save → load → clear → load reflects expected state
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from reyn.security.secrets.store import (
    clear_secret,
    list_secret_keys,
    load_secrets,
    save_secret,
)

# ── save + load ───────────────────────────────────────────────────────────────

def test_save_creates_file_and_sets_600(tmp_path):
    """Tier 2: save_secret creates the .env file with chmod 600."""
    secrets = tmp_path / "secrets.env"
    save_secret("MY_KEY", "my_value", path=secrets)

    assert secrets.exists()
    mode = secrets.stat().st_mode & 0o777
    assert mode == 0o600


def test_save_and_load_roundtrip(tmp_path):
    """Tier 2: saved secrets are readable back via load_secrets."""
    secrets = tmp_path / "secrets.env"
    save_secret("TOKEN_A", "aaa", path=secrets)
    save_secret("TOKEN_B", "bbb", path=secrets)

    data = load_secrets(path=secrets)
    assert data["TOKEN_A"] == "aaa"
    assert data["TOKEN_B"] == "bbb"


def test_save_updates_existing_key(tmp_path):
    """Tier 2: saving a key that already exists updates the value in-place."""
    secrets = tmp_path / "secrets.env"
    save_secret("EXISTING", "old_value", path=secrets)
    save_secret("OTHER", "stable", path=secrets)
    save_secret("EXISTING", "new_value", path=secrets)

    data = load_secrets(path=secrets)
    assert data["EXISTING"] == "new_value"
    assert data["OTHER"] == "stable"  # unaffected


def test_load_absent_file_returns_empty(tmp_path):
    """Tier 2: load_secrets returns {} when the file does not exist."""
    missing = tmp_path / "no_file.env"
    assert load_secrets(path=missing) == {}


# ── clear ─────────────────────────────────────────────────────────────────────

def test_clear_removes_key(tmp_path):
    """Tier 2: clear_secret removes the key and returns True."""
    secrets = tmp_path / "secrets.env"
    save_secret("TO_CLEAR", "value", path=secrets)
    save_secret("TO_KEEP", "keep", path=secrets)

    removed = clear_secret("TO_CLEAR", path=secrets)
    assert removed is True

    data = load_secrets(path=secrets)
    assert "TO_CLEAR" not in data
    assert data["TO_KEEP"] == "keep"


def test_clear_missing_key_returns_false(tmp_path):
    """Tier 2: clear_secret on a non-existent key returns False (no error, idempotent)."""
    secrets = tmp_path / "secrets.env"
    save_secret("REAL_KEY", "real", path=secrets)

    removed = clear_secret("GHOST_KEY", path=secrets)
    assert removed is False

    # Existing key unaffected
    data = load_secrets(path=secrets)
    assert data["REAL_KEY"] == "real"


def test_clear_absent_file_returns_false(tmp_path):
    """Tier 2: clear_secret on a missing file returns False without error."""
    missing = tmp_path / "no_file.env"
    assert clear_secret("ANY", path=missing) is False


# ── list_secret_keys ──────────────────────────────────────────────────────────

def test_list_returns_keys_only(tmp_path):
    """Tier 2: list_secret_keys returns key names without values."""
    secrets = tmp_path / "secrets.env"
    save_secret("KEY_ONE", "val1", path=secrets)
    save_secret("KEY_TWO", "val2", path=secrets)

    keys = list_secret_keys(path=secrets)
    assert "KEY_ONE" in keys
    assert "KEY_TWO" in keys
    # Values must NOT appear in the list
    assert "val1" not in keys
    assert "val2" not in keys


def test_list_absent_file_returns_empty(tmp_path):
    """Tier 2: list_secret_keys returns [] when the file does not exist."""
    missing = tmp_path / "no_file.env"
    assert list_secret_keys(path=missing) == []


def test_list_preserves_insertion_order(tmp_path):
    """Tier 2: list_secret_keys returns keys in declaration order."""
    secrets = tmp_path / "secrets.env"
    save_secret("ALPHA", "a", path=secrets)
    save_secret("BETA", "b", path=secrets)
    save_secret("GAMMA", "g", path=secrets)

    keys = list_secret_keys(path=secrets)
    assert keys == ["ALPHA", "BETA", "GAMMA"]


# ── full roundtrip ────────────────────────────────────────────────────────────

def test_full_roundtrip_save_load_clear_load(tmp_path):
    """Tier 2: end-to-end roundtrip: save → load → clear → load reflects expected state."""
    secrets = tmp_path / "secrets.env"

    save_secret("K1", "v1", path=secrets)
    save_secret("K2", "v2", path=secrets)

    data = load_secrets(path=secrets)
    assert data == {"K1": "v1", "K2": "v2"}

    clear_secret("K1", path=secrets)

    data2 = load_secrets(path=secrets)
    assert data2 == {"K2": "v2"}


def test_save_empty_key_raises(tmp_path):
    """Tier 2: saving a secret with an empty key raises ValueError."""
    secrets = tmp_path / "secrets.env"
    with pytest.raises(ValueError):
        save_secret("", "value", path=secrets)
