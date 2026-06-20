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


def test_valid_models_still_loads(tmp_path) -> None:
    """Tier 2: a well-formed models block is unaffected by the guard (regression)."""
    cfg = _load(tmp_path, "models:\n  light: openai/gpt-4o\n  standard: claude-sonnet\n")
    assert dict(cfg.models) == {"light": "openai/gpt-4o", "standard": "claude-sonnet"}
