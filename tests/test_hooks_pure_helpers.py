"""Tier 2: pure helpers in tools/hooks.py.

  ``_normalize_on(h)``    — YAML-1.1 bare ``on:`` → boolean True fixup
  ``_hooks_list(data)``   — extract and normalize the hooks list from yaml data
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tools.hooks import _hooks_list, _normalize_on

# ---------------------------------------------------------------------------
# _normalize_on
# ---------------------------------------------------------------------------


def test_normalize_on_plain_dict_unchanged() -> None:
    """Tier 2: dict without a True key is returned as-is."""
    h = {"on": "push", "name": "my-hook"}
    result = _normalize_on(h)
    assert result == {"on": "push", "name": "my-hook"}


def test_normalize_on_yaml11_true_key_renames_to_on() -> None:
    """Tier 2: YAML-1.1 {True: event} → {'on': event}."""
    h = {True: "push", "name": "ci"}
    result = _normalize_on(h)
    assert isinstance(result, dict)
    assert "on" in result
    assert result["on"] == "push"
    assert True not in result


def test_normalize_on_already_has_on_key_unchanged() -> None:
    """Tier 2: dict that already has 'on' key is left untouched even if True is present."""
    h = {True: "push", "on": "pull_request"}
    result = _normalize_on(h)
    assert result is h


def test_normalize_on_non_dict_passthrough() -> None:
    """Tier 2: non-dict values (str, list, None) pass through unchanged."""
    assert _normalize_on("string") == "string"
    assert _normalize_on(["a", "b"]) == ["a", "b"]
    assert _normalize_on(None) is None


def test_normalize_on_empty_dict_unchanged() -> None:
    """Tier 2: empty dict is returned as-is."""
    assert _normalize_on({}) == {}


# ---------------------------------------------------------------------------
# _hooks_list
# ---------------------------------------------------------------------------


def test_hooks_list_returns_normalized_hooks() -> None:
    """Tier 2: each hook entry is passed through _normalize_on."""
    data = {"hooks": [{"on": "push"}, {"on": "pull_request"}]}
    result = _hooks_list(data)
    assert {"on": "push"} in result
    assert {"on": "pull_request"} in result


def test_hooks_list_normalizes_yaml11_entries() -> None:
    """Tier 2: {True: event} entries in the list are normalized to {'on': event}."""
    data = {"hooks": [{True: "deploy", "name": "ci"}]}
    result = _hooks_list(data)
    assert result[0]["on"] == "deploy"
    assert True not in result[0]


def test_hooks_list_empty_hooks() -> None:
    """Tier 2: empty hooks list returns empty list."""
    assert _hooks_list({"hooks": []}) == []


def test_hooks_list_missing_hooks_key() -> None:
    """Tier 2: no 'hooks' key returns empty list."""
    assert _hooks_list({}) == []


def test_hooks_list_hooks_not_list() -> None:
    """Tier 2: hooks is not a list → empty list (defensive)."""
    assert _hooks_list({"hooks": "not-a-list"}) == []
