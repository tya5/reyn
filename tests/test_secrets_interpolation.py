"""Tier 2: OS invariant — generic ${VAR} interpolation in reyn.secrets.interpolation.

Pins the contract for ``expand_env()``:

  - Single string: ${VAR} is replaced by os.environ value
  - Undefined VAR: expands to "" with UserWarning
  - $$ escape: expands to literal "$"
  - Non-string scalars (int, bool, None): pass through unchanged
  - Nested dict: all string values at any depth are resolved
  - List: all string items are resolved recursively
  - Mixed nesting (dict of lists, list of dicts): correct resolution
  - mcp_client.expand_env re-export is backward-compatible
"""
from __future__ import annotations

import os
import warnings

import pytest

from reyn.secrets.interpolation import expand_env

# ── simple string ────────────────────────────────────────────────────────────

def test_expand_known_var(monkeypatch):
    """Tier 2: ${VAR} expands to the env value when the variable is set."""
    monkeypatch.setenv("REYN_INTERP_TEST", "hello_world")
    assert expand_env("prefix_${REYN_INTERP_TEST}_suffix") == "prefix_hello_world_suffix"


def test_expand_multiple_vars_in_one_string(monkeypatch):
    """Tier 2: multiple ${VAR} tokens in a single string are all expanded."""
    monkeypatch.setenv("REYN_A", "foo")
    monkeypatch.setenv("REYN_B", "bar")
    assert expand_env("${REYN_A}-${REYN_B}") == "foo-bar"


def test_undefined_var_expands_to_empty_with_warning(monkeypatch):
    """Tier 2: undefined ${VAR} expands to '' and emits UserWarning (no crash)."""
    monkeypatch.delenv("REYN_UNDEFINED_XYZ", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = expand_env("before_${REYN_UNDEFINED_XYZ}_after")

    assert result == "before__after"
    assert any(
        "REYN_UNDEFINED_XYZ" in str(w.message) for w in caught
    ), "Expected a warning mentioning the undefined variable"


def test_dollar_dollar_escape():
    """Tier 2: $$ expands to a literal '$' character."""
    assert expand_env("price: $$100") == "price: $100"
    assert expand_env("$$") == "$"


def test_dollar_dollar_before_var(monkeypatch):
    """Tier 2: $$ immediately before ${VAR} expands $$ first, leaving the VAR token intact."""
    monkeypatch.setenv("REYN_PRICE", "99")
    # "$$${REYN_PRICE}" → "$99"
    assert expand_env("$$${REYN_PRICE}") == "$99"


# ── non-string scalars ────────────────────────────────────────────────────────

def test_non_string_passthrough():
    """Tier 2: int, bool, None pass through expand_env unchanged."""
    assert expand_env(42) == 42
    assert expand_env(True) is True
    assert expand_env(None) is None
    assert expand_env(3.14) == 3.14


# ── nested dict ───────────────────────────────────────────────────────────────

def test_dict_values_expanded(monkeypatch):
    """Tier 2: all string values in a dict are expanded; non-string values pass through."""
    monkeypatch.setenv("REYN_KEY_HOST", "localhost")
    obj = {"host": "${REYN_KEY_HOST}", "port": 8080, "enabled": True}
    result = expand_env(obj)
    assert result == {"host": "localhost", "port": 8080, "enabled": True}


def test_nested_dict_expanded(monkeypatch):
    """Tier 2: ${VAR} in deeply nested dict values is resolved."""
    monkeypatch.setenv("REYN_NESTED_TOKEN", "secret_token")
    obj = {
        "outer": {
            "inner": {
                "auth": "Bearer ${REYN_NESTED_TOKEN}",
            }
        }
    }
    result = expand_env(obj)
    assert result["outer"]["inner"]["auth"] == "Bearer secret_token"


# ── list ─────────────────────────────────────────────────────────────────────

def test_list_items_expanded(monkeypatch):
    """Tier 2: ${VAR} in list items is resolved; non-string items pass through."""
    monkeypatch.setenv("REYN_LIST_VAL", "expanded")
    obj = ["plain", "${REYN_LIST_VAL}", 42, None]
    result = expand_env(obj)
    assert result == ["plain", "expanded", 42, None]


def test_list_of_dicts_expanded(monkeypatch):
    """Tier 2: nested list-of-dict structure is fully traversed."""
    monkeypatch.setenv("REYN_LD_KEY", "myvalue")
    obj = [{"key": "${REYN_LD_KEY}"}, {"other": "static"}]
    result = expand_env(obj)
    assert result == [{"key": "myvalue"}, {"other": "static"}]


# ── backward-compat re-export from mcp_client ────────────────────────────────

def test_mcp_client_expand_env_is_same_function(monkeypatch):
    """Tier 2: mcp_client.expand_env is the shared implementation — backward-compat."""
    from reyn.mcp_client import expand_env as mcp_expand
    monkeypatch.setenv("REYN_MCP_COMPAT", "compat_value")
    assert mcp_expand("${REYN_MCP_COMPAT}") == "compat_value"
