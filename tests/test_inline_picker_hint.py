"""Tier 2: _picker_hint — pure status-bar hint selector for the above-region picker.

The hint text shown in the status bar depends on whether the above-region
picker has focus and, if so, which kind of element is active (intervention
vs. command-UI). ``_picker_hint`` is extracted as a pure function so the
focus-resolution (``get_app().layout.has_focus``) stays in the PT layer while
the selection logic is unit-testable here.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import _picker_hint


def test_picker_hint_no_focus_returns_default():
    """Tier 2: when the above-region has no focus, return the default nav hint."""
    assert _picker_hint(False, None) == "  [↓ menu · ↑ history · /quit]"


def test_picker_hint_intervention_omits_esc():
    """Tier 2: intervention picker (iv: key) shows enter-confirm hint without esc.

    Escape is intentionally blocked for interventions — the session blocks
    until the user resolves the choice, so the hint must not suggest esc.
    """
    assert _picker_hint(True, "iv:abc123") == "  [↑↓ select · enter confirm]"


def test_picker_hint_command_ui_includes_esc():
    """Tier 2: command-UI picker (cmd: key) shows cancel hint with esc."""
    assert _picker_hint(True, "cmd:rewind-001") == "  [↑↓ select · enter · esc cancel]"
