"""Tier 2: #5100 malformed hooks warnings reach the existing chrome."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import config_warning_text


def test_malformed_hooks_warning_is_visible_without_hiding_config_warning() -> None:
    """Tier 2: both independent warning facts remain visible in one line."""
    text = config_warning_text(
        2,
        hooks_warnings=["hooks.yaml could not be read: hooks.yaml (line 3, column 9)"],
    )
    assert "hooks.yaml" in text
    assert "line 3" in text
    assert "2 config keys not applied" in text
    assert "turn_end" not in text


def test_all_hooks_warning_paths_are_visible() -> None:
    """Tier 2: a bounded path-keyed warning collection is not reduced to its first item."""
    text = config_warning_text(
        0,
        hooks_warnings=["hooks.yaml could not be read: a.yaml (line 1, column 1)", "hooks.yaml could not be read: b.yaml (line 2, column 1)"],
    )
    assert "a.yaml" in text
    assert "b.yaml" in text


def test_healthy_hooks_have_no_warning_line() -> None:
    """Tier 2: no malformed hooks warning preserves the existing no-indicator behavior."""
    assert config_warning_text(0, hooks_warnings=[]) is None
