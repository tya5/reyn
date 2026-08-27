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


def test_unreported_connection_with_empty_warnings_says_so() -> None:
    """Tier 2: #5100/#5272 (lead-coder correction) — an empty hooks_warnings
    list on a connection that CANNOT report them (hooks_warnings_reported=
    False, e.g. a remote AG-UI connection) must not read as "healthy" — a
    genuinely empty list and an unreportable one are different facts.
    Real incident this closes: a broken per-session hooks.yaml on the
    server side of a remote connection previously showed no indicator at
    all, indistinguishable from a clean config."""
    text = config_warning_text(0, hooks_warnings=[], hooks_warnings_reported=False)
    assert text is not None
    assert "not reported" in text


def test_default_reported_true_preserves_pre_5272_behavior() -> None:
    """Tier 2: callers that don't pass hooks_warnings_reported (every call
    site before #5272, and every LOCAL session today) get the exact
    pre-#5272 behavior — no new line appears out of nowhere for an
    unmodified caller."""
    assert config_warning_text(0, hooks_warnings=[]) is None
    assert config_warning_text(0, hooks_warnings=[]) == config_warning_text(
        0, hooks_warnings=[], hooks_warnings_reported=True,
    )


def test_real_warning_content_wins_over_unreported_marker() -> None:
    """Tier 2: genuine warning content is never suppressed by the
    unreported marker, even if a caller passes hooks_warnings_reported=
    False alongside real content (a real warning always outranks "can't
    tell")."""
    text = config_warning_text(
        0,
        hooks_warnings=["hooks.yaml could not be read: a.yaml (line 1, column 1)"],
        hooks_warnings_reported=False,
    )
    assert "a.yaml" in text
    assert "not reported" not in text
