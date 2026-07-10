"""Tier 2: multiline inline-input pure helpers (owner spec: Enter=submit,
Shift+Enter=newline).

``_is_shift_enter_escape`` and ``_down_arrow_action`` are extracted as pure
functions so the terminal-escape-sequence detection and the row-aware ↓-key
decision are unit-testable without a running prompt_toolkit Application —
mirrors the existing ``_picker_hint`` extraction pattern in this module.

The Shift+Enter detection is grounded in a real cross-terminal investigation
(direct prompt_toolkit source read + an empirical tmux byte-probe sending the
literal escape sequences into a live prompt_toolkit key processor): most
terminals send an identical "\\r" for Enter and Shift+Enter (a hard VT100
protocol limit), but mintty (Windows Git Bash) sends a disambiguated xterm
modifyOtherKeys escape sequence by default, which prompt_toolkit's own
``ansi_escape_sequences.py`` collapses back to plain Enter at the ``.key``
level while still preserving the original bytes in ``.data``.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import (
    _INPUT_MAX_HEIGHT,
    _SHIFT_ENTER_RAW_DATA,
    _down_arrow_action,
    _input_window_height,
    _is_shift_enter_escape,
)


def test_is_shift_enter_escape_true_for_mintty_sequence():
    """Tier 2: the mintty/xterm-modifyOtherKeys Shift+Enter escape sequence is
    recognized as Shift+Enter."""
    assert _is_shift_enter_escape("\x1b[27;2;13~") is True


def test_is_shift_enter_escape_false_for_plain_enter():
    """Tier 2: plain Enter's raw data ("\\r") is NOT mistaken for Shift+Enter —
    the whole point of the detection is telling these two apart."""
    assert _is_shift_enter_escape("\r") is False


def test_is_shift_enter_escape_false_for_unrelated_data():
    """Tier 2: arbitrary other key data (e.g. a normal character) is not
    misclassified as Shift+Enter."""
    assert _is_shift_enter_escape("a") is False
    assert _is_shift_enter_escape("") is False


def test_shift_enter_raw_data_set_is_nonempty():
    """Tier 2: the recognized-sequences set actually has the mintty entry (a
    regression guard against the constant being accidentally emptied)."""
    assert "\x1b[27;2;13~" in _SHIFT_ENTER_RAW_DATA


def test_down_arrow_action_empty_buffer_focuses_status():
    """Tier 2: ↓ on an empty input box drops focus to the status bar (the
    existing discoverable affordance, unchanged by multiline support)."""
    assert _down_arrow_action(has_text=False, cursor_row=0, line_count=1) == "focus_status"


def test_down_arrow_action_single_line_with_text_goes_to_history():
    """Tier 2: single-line buffer (cursor_row == line_count - 1 == 0) with text
    falls to history-forward — byte-identical to the pre-multiline behavior."""
    action = _down_arrow_action(has_text=True, cursor_row=0, line_count=1)
    assert action == "history_forward"


def test_down_arrow_action_multiline_not_on_last_row_moves_cursor():
    """Tier 2: multiline buffer, cursor NOT on the last line → move the cursor
    down a line (in-buffer navigation), not history — this is the new
    behavior multiline support requires (mirrors Buffer.auto_down's own
    row-check, since the custom ↓ binding replaces auto_down entirely)."""
    action = _down_arrow_action(has_text=True, cursor_row=0, line_count=3)
    assert action == "cursor_down"


def test_down_arrow_action_multiline_on_last_row_goes_to_history():
    """Tier 2: multiline buffer, cursor ON the last line → falls through to
    history-forward, same as the single-line case."""
    action = _down_arrow_action(has_text=True, cursor_row=2, line_count=3)
    assert action == "history_forward"


def test_down_arrow_action_multiline_middle_row_moves_cursor():
    """Tier 2: a middle row (neither first nor last) always moves the cursor,
    never triggers history or status-bar focus."""
    action = _down_arrow_action(has_text=True, cursor_row=1, line_count=5)
    assert action == "cursor_down"


def test_input_window_height_single_line_is_one():
    """Tier 2: a single-line buffer (the pre-multiline common case) still gets
    exactly height=1 — byte-identical to the old fixed height=1 default."""
    assert _input_window_height(1) == 1


def test_input_window_height_grows_with_line_count():
    """Tier 2: the input window height grows with the buffer's line count —
    this is the actual fix for the review-caught rendering gap: a fixed
    height=1 window only ever showed the buffer's CURRENT line, hiding every
    earlier line once Shift+Enter/Ctrl+J could insert real newlines (confirmed
    live via tmux: "line one" + Shift+Enter + "line two" showed only "line
    two" even though the newline itself inserted correctly)."""
    assert _input_window_height(3) == 3


def test_input_window_height_capped_at_max():
    """Tier 2: height never exceeds _INPUT_MAX_HEIGHT, regardless of how many
    lines the buffer holds — same "Window too small" guard rationale as the
    existing _ABOVE_REGION_MAX_HEIGHT / _MENU_REGION_MAX_HEIGHT caps."""
    assert _input_window_height(_INPUT_MAX_HEIGHT + 50) == _INPUT_MAX_HEIGHT


def test_input_window_height_never_below_one():
    """Tier 2: a defensive floor — line_count should never be < 1 in practice,
    but the height must never go to 0 (an invisible input box) even so."""
    assert _input_window_height(0) == 1
