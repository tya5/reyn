"""Tier 1: Contract tests for ScopedSecretStore and CredentialScopeError (FP-0016 Component D).

Pins the public API surface of ScopedSecretStore as exported from reyn.secrets:

  - get(key) returns value when key is allowed and present
  - get(key) returns default when key is allowed but absent
  - get(key) raises CredentialScopeError when key is NOT in allowed_keys
  - Unrestricted ("*") allows any key to read through
  - __contains__ returns False for disallowed keys (no raise)
  - __contains__ returns False for allowed-but-absent keys
  - __contains__ returns True for allowed-and-present keys
  - list_visible_keys() returns only allowed AND present keys (no leak)
  - list_visible_keys() under unrestricted returns all store keys
  - allowed_keys is a frozenset (immutable)
  - is_unrestricted is True iff "*" in allowed_keys
  - CredentialScopeError is a PermissionError subclass
  - Error message names the disallowed key
  - Empty allowed set — every read raises CredentialScopeError
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.secrets import CredentialScopeError, ScopedSecretStore


def _write_dotenv(tmp_path: Path, pairs: list[tuple[str, str]]) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text("\n".join(f"{k}={v}" for k, v in pairs))
    return p


# ── Case 1: get returns value when key allowed and present ────────────────────

def test_get_allowed_present_returns_value(tmp_path):
    """Tier 1: get(key) returns value when key is in allowed_keys and present in store."""
    p = _write_dotenv(tmp_path, [("API_KEY", "secret123"), ("OTHER", "other_val")])
    store = ScopedSecretStore(allowed_keys=["API_KEY"], path=p)
    assert store.get("API_KEY") == "secret123"


# ── Case 2: get returns default when key allowed but absent ───────────────────

def test_get_allowed_absent_returns_default(tmp_path):
    """Tier 1: get(key) returns default when key is in allowed_keys but not in store."""
    p = _write_dotenv(tmp_path, [("OTHER", "other_val")])
    store = ScopedSecretStore(allowed_keys=["MISSING_KEY", "OTHER"], path=p)
    assert store.get("MISSING_KEY") is None
    assert store.get("MISSING_KEY", "fallback") == "fallback"


# ── Case 3: get raises CredentialScopeError for disallowed key ────────────────

def test_get_disallowed_raises_scope_error(tmp_path):
    """Tier 1: get(key) raises CredentialScopeError when key is NOT in allowed_keys."""
    p = _write_dotenv(tmp_path, [("API_KEY", "secret"), ("FORBIDDEN", "hidden")])
    store = ScopedSecretStore(allowed_keys=["API_KEY"], path=p)
    with pytest.raises(CredentialScopeError):
        store.get("FORBIDDEN")


# ── Case 4: unrestricted ("*") allows any key to pass through ─────────────────

def test_unrestricted_get_allows_any_key(tmp_path):
    """Tier 1: allowed_keys=["*"] makes any key readable; unknown keys return default."""
    p = _write_dotenv(tmp_path, [("SECRET_A", "val_a"), ("SECRET_B", "val_b")])
    store = ScopedSecretStore(allowed_keys=["*"], path=p)
    assert store.get("SECRET_A") == "val_a"
    assert store.get("SECRET_B") == "val_b"
    # Unknown key returns default without raising
    assert store.get("UNKNOWN_KEY") is None
    assert store.get("UNKNOWN_KEY", "default_val") == "default_val"


# ── Case 5: __contains__ returns False for disallowed keys (no raise) ─────────

def test_contains_disallowed_returns_false_no_raise(tmp_path):
    """Tier 1: __contains__ returns False for disallowed keys without raising."""
    p = _write_dotenv(tmp_path, [("ALLOWED", "val"), ("FORBIDDEN", "hidden")])
    store = ScopedSecretStore(allowed_keys=["ALLOWED"], path=p)
    # Must not raise even though key is present in underlying store
    result = "FORBIDDEN" in store
    assert result is False


# ── Case 6: __contains__ returns False for allowed-but-absent keys ────────────

def test_contains_allowed_absent_returns_false(tmp_path):
    """Tier 1: __contains__ returns False for a key that is allowed but absent from store."""
    p = _write_dotenv(tmp_path, [("PRESENT", "val")])
    store = ScopedSecretStore(allowed_keys=["NOT_IN_STORE", "PRESENT"], path=p)
    assert ("NOT_IN_STORE" in store) is False


# ── Case 7: __contains__ returns True for allowed-and-present keys ────────────

def test_contains_allowed_present_returns_true(tmp_path):
    """Tier 1: __contains__ returns True for a key that is allowed and present in store."""
    p = _write_dotenv(tmp_path, [("API_KEY", "secret"), ("OTHER", "other")])
    store = ScopedSecretStore(allowed_keys=["API_KEY"], path=p)
    assert ("API_KEY" in store) is True


# ── Case 8: list_visible_keys returns only allowed AND present keys ───────────

def test_list_visible_keys_no_leak(tmp_path):
    """Tier 1: list_visible_keys() returns only keys both allowed and present; no leakage of other keys."""
    p = _write_dotenv(tmp_path, [
        ("ALLOWED_PRESENT", "val1"),
        ("FORBIDDEN", "hidden"),
        ("ANOTHER_ALLOWED_PRESENT", "val2"),
    ])
    store = ScopedSecretStore(
        allowed_keys=["ALLOWED_PRESENT", "ANOTHER_ALLOWED_PRESENT", "ALLOWED_ABSENT"],
        path=p,
    )
    visible = store.list_visible_keys()
    assert "ALLOWED_PRESENT" in visible
    assert "ANOTHER_ALLOWED_PRESENT" in visible
    # Not in allowed_keys — must not appear
    assert "FORBIDDEN" not in visible
    # Allowed but absent from store — must not appear
    assert "ALLOWED_ABSENT" not in visible


# ── Case 9: list_visible_keys under unrestricted returns all store keys ────────

def test_list_visible_keys_unrestricted_returns_all(tmp_path):
    """Tier 1: list_visible_keys() with allowed_keys=["*"] returns all keys in the store."""
    p = _write_dotenv(tmp_path, [("KEY_A", "a"), ("KEY_B", "b"), ("KEY_C", "c")])
    store = ScopedSecretStore(allowed_keys=["*"], path=p)
    visible = store.list_visible_keys()
    assert set(visible) == {"KEY_A", "KEY_B", "KEY_C"}


# ── Case 10: allowed_keys is a frozenset (immutable) ─────────────────────────

def test_allowed_keys_is_frozenset(tmp_path):
    """Tier 1: allowed_keys property returns a frozenset."""
    p = _write_dotenv(tmp_path, [])
    store = ScopedSecretStore(allowed_keys=["KEY_A", "KEY_B"], path=p)
    assert isinstance(store.allowed_keys, frozenset)


# ── Case 11: is_unrestricted is True iff "*" in allowed_keys ─────────────────

def test_is_unrestricted_true_when_star_present(tmp_path):
    """Tier 1: is_unrestricted returns True when '*' is in allowed_keys."""
    p = _write_dotenv(tmp_path, [])
    store_unrestricted = ScopedSecretStore(allowed_keys=["*"], path=p)
    assert store_unrestricted.is_unrestricted is True

    store_restricted = ScopedSecretStore(allowed_keys=["KEY_A", "KEY_B"], path=p)
    assert store_restricted.is_unrestricted is False

    store_empty = ScopedSecretStore(allowed_keys=[], path=p)
    assert store_empty.is_unrestricted is False


# ── Case 12: CredentialScopeError is a PermissionError subclass ──────────────

def test_credential_scope_error_is_permission_error():
    """Tier 1: CredentialScopeError is a subclass of PermissionError (existing PermissionResolver except will still catch it)."""
    assert issubclass(CredentialScopeError, PermissionError)
    err = CredentialScopeError("test message")
    assert isinstance(err, PermissionError)


# ── Case 13: Error message names the key ─────────────────────────────────────

def test_scope_error_message_names_key(tmp_path):
    """Tier 1: CredentialScopeError message contains the name of the disallowed key."""
    p = _write_dotenv(tmp_path, [("ALLOWED", "val"), ("FORBIDDEN_KEY", "hidden")])
    store = ScopedSecretStore(allowed_keys=["ALLOWED"], path=p)
    with pytest.raises(CredentialScopeError) as exc_info:
        store.get("FORBIDDEN_KEY")
    assert "FORBIDDEN_KEY" in str(exc_info.value)


# ── Case 14: Empty allowed set — every read raises CredentialScopeError ───────

def test_empty_allowed_set_every_get_raises(tmp_path):
    """Tier 1: ScopedSecretStore with allowed_keys=[] raises CredentialScopeError for every get call."""
    p = _write_dotenv(tmp_path, [("ANY_KEY", "any_val")])
    store = ScopedSecretStore(allowed_keys=[], path=p)
    with pytest.raises(CredentialScopeError):
        store.get("ANY_KEY")
    with pytest.raises(CredentialScopeError):
        store.get("ANOTHER_KEY")
    # list_visible_keys should return empty (nothing allowed)
    assert store.list_visible_keys() == []
    # __contains__ should return False (not raise) even for empty scope
    assert ("ANY_KEY" in store) is False
