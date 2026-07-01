"""Tier 2: llm/llm.py G12-signal pure helper contracts.

_is_g12_error_status(status) classifies a dispatch-result status string as
an explicit error.

_trailing_tool_is_error(content) classifies a JSON tool-result payload as
an error, checking the dispatch-level status and the nested data.status.
Both are used by the G12 signal to distinguish errored trailing tool calls
from successful ones.
"""
from __future__ import annotations

import json

from reyn.llm.llm import _is_g12_error_status, _trailing_tool_is_error

# ── _is_g12_error_status ──────────────────────────────────────────────────────


def test_is_g12_error_status_error() -> None:
    """Tier 2: 'error' is in the G12 error set."""
    assert _is_g12_error_status("error") is True


def test_is_g12_error_status_denied() -> None:
    """Tier 2: 'denied' is in the G12 error set."""
    assert _is_g12_error_status("denied") is True


def test_is_g12_error_status_not_found() -> None:
    """Tier 2: 'not_found' is in the G12 error set."""
    assert _is_g12_error_status("not_found") is True


def test_is_g12_error_status_failed() -> None:
    """Tier 2: 'failed' is in the G12 error set."""
    assert _is_g12_error_status("failed") is True


def test_is_g12_error_status_case_insensitive() -> None:
    """Tier 2: matching is case-insensitive ('ERROR' → True)."""
    assert _is_g12_error_status("ERROR") is True
    assert _is_g12_error_status("Denied") is True


def test_is_g12_error_status_ok_returns_false() -> None:
    """Tier 2: 'ok' is not an error status."""
    assert _is_g12_error_status("ok") is False


def test_is_g12_error_status_none_returns_false() -> None:
    """Tier 2: None is not a string — returns False without raising."""
    assert _is_g12_error_status(None) is False


def test_is_g12_error_status_non_string_returns_false() -> None:
    """Tier 2: integer input returns False (isinstance guard)."""
    assert _is_g12_error_status(42) is False


# ── _trailing_tool_is_error ───────────────────────────────────────────────────


def test_trailing_tool_is_error_dispatch_level_error() -> None:
    """Tier 2: top-level status='error' is detected."""
    payload = json.dumps({"status": "error", "data": {}})
    assert _trailing_tool_is_error(payload) is True


def test_trailing_tool_is_error_dispatch_level_denied() -> None:
    """Tier 2: top-level status='denied' is detected."""
    payload = json.dumps({"status": "denied"})
    assert _trailing_tool_is_error(payload) is True


def test_trailing_tool_is_error_nested_data_error() -> None:
    """Tier 2: op-execution error nested under data.status is detected."""
    payload = json.dumps({"status": "ok", "data": {"status": "error", "path": "x"}})
    assert _trailing_tool_is_error(payload) is True


def test_trailing_tool_is_error_nested_data_not_found() -> None:
    """Tier 2: data.status='not_found' (e.g. file read failure) is detected."""
    payload = json.dumps({"status": "ok", "data": {"status": "not_found"}})
    assert _trailing_tool_is_error(payload) is True


def test_trailing_tool_is_error_ok_at_both_levels_returns_false() -> None:
    """Tier 2: status='ok' at both levels → False (success cell)."""
    payload = json.dumps({"status": "ok", "data": {"status": "ok"}})
    assert _trailing_tool_is_error(payload) is False


def test_trailing_tool_is_error_non_json_returns_false() -> None:
    """Tier 2: non-JSON content returns False without raising."""
    assert _trailing_tool_is_error("plain text result") is False


def test_trailing_tool_is_error_not_starting_with_brace_returns_false() -> None:
    """Tier 2: content not starting with '{' returns False (fast-path guard)."""
    assert _trailing_tool_is_error("some text {status: error}") is False


def test_trailing_tool_is_error_malformed_json_returns_false() -> None:
    """Tier 2: malformed JSON starting with '{' returns False without raising."""
    assert _trailing_tool_is_error("{invalid json") is False


def test_trailing_tool_is_error_empty_returns_false() -> None:
    """Tier 2: empty string returns False."""
    assert _trailing_tool_is_error("") is False
