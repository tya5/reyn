"""Tier 2: load_config survives a malformed top-level value (no uncaught crash).

Found via bug-mining (2026-06-20). `models:` or `permissions:` written as a
scalar/list in reyn.yaml (a user typo) crashed `load_config` with an uncaught
`AttributeError` (`.items()` on a str) / `ValueError` (`dict()` on a non-pair
list) — `(merged.get("models") or {})` guards None but not a truthy non-dict.

A config loader must degrade gracefully on operator misconfiguration: the
malformed block defaults to empty (with a warning), and a valid config in the
same file still loads.

Falsification: pre-fix each malformed case raised; the valid-config test proves
the guard doesn't swallow good config.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config import load_config


def _load(tmp_path: Path, yaml_text: str):
    (tmp_path / "reyn.yaml").write_text(yaml_text, encoding="utf-8")
    return load_config(tmp_path)


def test_models_as_string_does_not_crash(tmp_path) -> None:
    """Tier 2: `models: <string>` defaults to empty instead of crashing."""
    cfg = _load(tmp_path, "models: not-a-dict\n")
    assert dict(cfg.models) == {}


def test_models_as_list_does_not_crash(tmp_path) -> None:
    """Tier 2: `models: [..]` defaults to empty instead of crashing."""
    cfg = _load(tmp_path, "models: [1, 2, 3]\n")
    assert dict(cfg.models) == {}


def test_permissions_as_list_does_not_crash(tmp_path) -> None:
    """Tier 2: `permissions: [..]` defaults to empty instead of crashing.

    Falsification: pre-fix `dict([a, b])` raised ValueError (non-pair list).
    """
    cfg = _load(tmp_path, "permissions: [a, b]\n")
    assert cfg.permissions == {}


def test_mcp_as_string_does_not_crash(tmp_path) -> None:
    """Tier 2: `mcp: <string>` defaults to empty instead of an unclear crash.

    Falsification: pre-fix `dict("str")` raised ValueError (non-pair sequence).
    """
    cfg = _load(tmp_path, "mcp: not-a-dict\n")
    assert cfg.mcp == {}


def test_mcp_as_list_does_not_crash(tmp_path) -> None:
    """Tier 2: `mcp: [..]` defaults to empty instead of crashing."""
    cfg = _load(tmp_path, "mcp: [1, 2, 3]\n")
    assert cfg.mcp == {}


def test_tool_calls_op_loop_skills_non_list_does_not_crash(tmp_path) -> None:
    """Tier 2: a non-list `tool_calls_op_loop_skills` defaults to empty.

    Falsification: pre-fix `for s in 42` raised TypeError, and a string
    silently iterated into garbage per-character entries.
    """
    cfg_int = _load(tmp_path, "tool_calls_op_loop_skills: 42\n")
    assert cfg_int.tool_calls_op_loop_skills == []
    cfg_str = _load(tmp_path, "tool_calls_op_loop_skills: oops\n")
    assert cfg_str.tool_calls_op_loop_skills == []


def test_valid_models_still_loads(tmp_path) -> None:
    """Tier 2: a well-formed models block is unaffected by the guard (regression)."""
    cfg = _load(tmp_path, "models:\n  light: openai/gpt-4o\n  standard: claude-sonnet\n")
    assert dict(cfg.models) == {"light": "openai/gpt-4o", "standard": "claude-sonnet"}


def test_fail_loud_sections_still_raise_clear_error(tmp_path) -> None:
    """Tier 2: the intentional fail-loud sections are NOT made lenient.

    `time_travel` (a cost/durability knob) deliberately raises a clear
    'must be a mapping' ValueError on a malformed value — the resilience guard
    must not silence that distinct convention. Falsification: if the guard were
    over-applied to the builders, this would load instead of raising.
    """
    import pytest
    (tmp_path / "reyn.yaml").write_text("time_travel: not-a-dict\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(tmp_path)
