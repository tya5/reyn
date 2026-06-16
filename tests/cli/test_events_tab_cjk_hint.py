"""Tier 2b: ``_event_hint`` CJK cell-aware truncation at 40-cell budget.

Wave-13 narrow-terminal regression. Bug: multiple ``_event_hint`` branches
used ``text[:N]`` codepoint slicing (+ ``len(text) > N`` guard). CJK
characters consume 2 cells each, so 40 codepoints = 80 cells — exactly
2× the 40-cell target. At 80-col panel width this caused row wrapping and
``event_ys`` cursor misalignment.

Fix: 6 affected branches now call ``_truncate_to_cells(text, N)`` which is
East-Asian-Width aware via ``rich.cells.cell_len``. This test pins the
cell-width contract for each affected event type.

Cell budget contract: hint cell_width <= 41 (= 40 cells of content + 1
cell for the ``…`` ellipsis character, or <= 40 cells if no truncation
was needed).

Tier self-check:
  - No MagicMock / AsyncMock / patch
  - Docstrings declare Tier 2b
  - Tests exercise the public ``_event_hint`` function directly
  - No private-state assertions
  - No snapshot / golden-file output
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.right_panel.events_tab import _event_hint  # noqa: E402

# 41 = 40 content cells + 1 ellipsis cell (the "…" glyph is 1 cell wide).
_MAX_HINT_CELLS = 41


def test_user_message_received_cjk_fits_cell_budget() -> None:
    """Tier 2b: CJK user_message_received hint stays within 40+1 cells."""
    # 45 CJK chars = 90 cells if untruncated
    ev = {"type": "user_message_received", "data": {"text": "今日はどうですか？" * 5}}
    hint = _event_hint(ev)
    width = cell_len(hint)
    assert width <= _MAX_HINT_CELLS, (
        f"user_message_received CJK hint cell_width={width} > {_MAX_HINT_CELLS}: {hint!r}"
    )


def test_user_message_received_ascii_passes_through() -> None:
    """Tier 2b: short ASCII user_message_received is returned unchanged."""
    ev = {"type": "user_message_received", "data": {"text": "hello world"}}
    hint = _event_hint(ev)
    assert hint == "hello world"
    assert cell_len(hint) <= _MAX_HINT_CELLS


def test_workflow_aborted_cjk_fits_cell_budget() -> None:
    """Tier 2b: CJK workflow_aborted reason hint stays within 40+1 cells."""
    # 50 CJK chars = 100 cells if untruncated
    ev = {"type": "workflow_aborted", "data": {"reason": "エラーが発生しました" * 6}}
    hint = _event_hint(ev)
    width = cell_len(hint)
    assert width <= _MAX_HINT_CELLS, (
        f"workflow_aborted CJK hint cell_width={width} > {_MAX_HINT_CELLS}: {hint!r}"
    )


def test_user_intervention_requested_cjk_fits_cell_budget() -> None:
    """Tier 2b: CJK user_intervention_requested question hint stays within 40+1 cells."""
    ev = {
        "type": "user_intervention_requested",
        "data": {"question": "どのファイルを編集しますか？" * 4},
    }
    hint = _event_hint(ev)
    width = cell_len(hint)
    assert width <= _MAX_HINT_CELLS, (
        f"user_intervention_requested CJK hint cell_width={width} > {_MAX_HINT_CELLS}: {hint!r}"
    )


def test_user_intervention_received_cjk_fits_cell_budget() -> None:
    """Tier 2b: CJK user_intervention_received answer hint stays within 40+1 cells."""
    ev = {
        "type": "user_intervention_received",
        "data": {"answer": "はい、そのファイルを編集してください" * 3},
    }
    hint = _event_hint(ev)
    width = cell_len(hint)
    assert width <= _MAX_HINT_CELLS, (
        f"user_intervention_received CJK hint cell_width={width} > {_MAX_HINT_CELLS}: {hint!r}"
    )


def test_web_search_started_cjk_fits_cell_budget() -> None:
    """Tier 2b: CJK web_search_started query hint stays within 40+1 cells."""
    ev = {"type": "web_search_started", "data": {"query": "機械学習の最新トレンド" * 5}}
    hint = _event_hint(ev)
    width = cell_len(hint)
    assert width <= _MAX_HINT_CELLS, (
        f"web_search_started CJK hint cell_width={width} > {_MAX_HINT_CELLS}: {hint!r}"
    )


def test_tool_failed_cjk_does_not_overflow_message() -> None:
    """Tier 2b: CJK tool_failed hint truncates the message portion with ellipsis."""
    # tool_failed uses a 25-cell cap for the message portion.
    # A long CJK message that was previously sliced by codepoint must be truncated
    # cell-awaredly — confirm the message ends with "…" (= truncation happened).
    ev = {
        "type": "tool_failed",
        "data": {"tool": "bash", "message": "エラーメッセージ" * 5},
    }
    hint = _event_hint(ev)
    # Prefix is present.
    assert hint.startswith("bash:"), f"tool prefix missing: {hint!r}"
    # Truncation happened (= the CJK message was wide enough to trigger it).
    assert "…" in hint, (
        f"Expected ellipsis in tool_failed CJK hint (truncation must occur): {hint!r}"
    )


def test_event_hint_ellipsis_present_on_cjk_truncation() -> None:
    """Tier 2b: truncated CJK hints end with the ellipsis character '…'."""
    ev = {"type": "user_message_received", "data": {"text": "今日はどうですか？" * 5}}
    hint = _event_hint(ev)
    assert hint.endswith("…"), (
        f"Truncated CJK hint should end with ellipsis: {hint!r}"
    )
