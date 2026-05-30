"""Tier 2: Every character in matrix._CHARS is single-cell-width (cell_len == 1).

The Matrix rain render loop allocates exactly one terminal cell per column
per row.  Any East-Asian-Wide character (cell_len == 2) placed in _CHARS
would overwrite the adjacent column's cell, producing a double-wide smear
on most terminals.  This test enforces the single-width invariant to prevent
wide chars from sneaking back into _CHARS.

Tier self-check:
  - No MagicMock / AsyncMock / patch
  - Docstring declares Tier 2
  - Exercises the public module-level constant _CHARS directly
  - No private-state assertions
  - No snapshot / golden-file output
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.matrix import _CHARS  # noqa: E402


def test_all_chars_are_single_cell_width() -> None:
    """Tier 2: Every char in _CHARS must have cell_len == 1 (no East-Asian-Wide glyphs)."""
    widths = [(ch, cell_len(ch)) for ch in _CHARS]
    wide = [(ch, w) for ch, w in widths if w != 1]
    assert wide == [], (
        f"_CHARS contains {len(wide)} double-width character(s) that would cause "
        f"column bleed in the Matrix rain render loop: {wide}"
    )


def test_chars_is_non_empty() -> None:
    """Tier 2: _CHARS must be non-empty so the rain animation has glyphs to draw."""
    assert len(_CHARS) > 0, "_CHARS is empty — Matrix rain has no characters to render"
