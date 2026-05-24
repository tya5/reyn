"""Tier 2b: events tab ``↳ <reply>`` preview truncates by cell width, not char count.

The previous implementation sliced replies with ``reply[:72]`` — Python
character count, NOT terminal cell width. CJK and other East-Asian-Wide
characters consume 2 cells each, so a 72-char Japanese reply rendered
at ~144 cells and wrapped across 2-3 panel rows.

The fix uses ``_truncate_to_cells`` (East-Asian-Width aware via
``rich.cells.cell_len``) to cap the preview at 40 cells. This test
pins the helper's contract directly — the rendering integration is
already exercised by the events-tab smoke paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.right_panel.events_tab import _truncate_to_cells


def test_short_ascii_passes_through_untruncated() -> None:
    """Tier 2b: input <= cap is returned unchanged with was_truncated=False."""
    out, truncated = _truncate_to_cells("hello world", 40)
    assert out == "hello world"
    assert truncated is False


def test_long_ascii_truncates_to_cap_cells() -> None:
    """Tier 2b: ASCII input longer than cap is sliced to exactly cap cells."""
    src = "x" * 80
    out, truncated = _truncate_to_cells(src, 40)
    cells = cell_len(out)
    assert cells <= 40
    assert cells > 0
    assert truncated is True


def test_cjk_truncates_at_cell_boundary_not_char_count() -> None:
    """Tier 2b: CJK reply caps at the cell budget (= half the char count).

    The pre-fix bug: ``reply[:72]`` allowed 72 CJK chars × 2 cells = 144
    cells through, wrapping the preview line. The new helper must cap
    a long CJK string at <= 40 cells regardless of char count.
    """
    # 50 CJK chars (= 100 cells if unbounded)
    src = "あいうえお" * 10
    out, truncated = _truncate_to_cells(src, 40)
    cells = cell_len(out)
    assert cells <= 40, (
        f"CJK truncation must respect cell budget; got {cells} cells from {out!r}"
    )
    assert truncated is True
    # And the slice happens at a char boundary (no half-glyph).
    assert all(ord(ch) > 0x7F for ch in out), out


def test_mixed_ascii_and_cjk_respects_cell_budget() -> None:
    """Tier 2b: mixed-width content is truncated by cumulative cell count.

    Defends against a regression where the helper assumed uniform width.
    """
    # 4 ASCII (4 cells) + 30 CJK (60 cells) = 64 cells total.
    src = "ABCD" + ("漢" * 30)
    out, truncated = _truncate_to_cells(src, 40)
    cells = cell_len(out)
    assert cells <= 40
    assert truncated is True
    # The first 4 ASCII chars survive (= the helper isn't blindly
    # truncating at byte 40 or similar).
    assert out.startswith("ABCD"), out


def test_empty_input_returns_empty_no_truncation() -> None:
    """Tier 2b: edge — empty string is passed through unchanged."""
    out, truncated = _truncate_to_cells("", 40)
    assert out == ""
    assert truncated is False
