"""Tier 2b: cell-aware truncation of CJK user content in agents/pending tabs.

Sibling-completeness for the events-tab cell-aware-truncation class: the
agents-tab plan `goal` and the pending-tab intervention `summary`/`detail`
still used codepoint slicing (`text[:60]`). CJK characters are 2 cells each, so
`text[:60]` overshoots the column budget by up to 2× and wraps the narrow
panel rows (relevant for Japanese plan goals / intervention prompts).

Fix: a shared `truncate_to_cells` helper (in `_text_util`) applied to all
sites. These tests pin the helper contract + the pending-tab integration.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui._text_util import truncate_to_cells
from reyn.interfaces.tui.widgets.right_panel.pending_tab import render_pending


@dataclass(frozen=True)
class _View:
    id: str
    kind: str
    origin_channel_id: str
    created_at: str
    summary: str
    detail: str = ""


def _plain(markup: str) -> str:
    return "\n".join(Text.from_markup(line).plain for line in markup.split("\n"))


def test_truncate_to_cells_cjk_bounded() -> None:
    """Tier 2b: a wide CJK string truncates to ≤ budget cells + ellipsis."""
    s = "あ" * 50  # 100 cells
    out = truncate_to_cells(s, 60)
    assert cell_len(out) <= 61, f"cell_width={cell_len(out)} > 61: {out!r}"
    assert out.endswith("…")


def test_truncate_to_cells_ascii_passthrough() -> None:
    """Tier 2b: a string already within budget is returned unchanged."""
    assert truncate_to_cells("hello world", 60) == "hello world"
    assert truncate_to_cells("", 60) == ""


def test_pending_cjk_summary_does_not_overflow() -> None:
    """Tier 2b: a CJK intervention summary is cell-truncated, not codepoint-sliced.

    Before the fix `summary[:60]` of CJK = up to 120 cells, overflowing the
    panel row. The summary line must stay within (60 + ellipsis + chrome).
    """
    view = _View(
        id="iv12345678", kind="intervention", origin_channel_id="user",
        created_at="2026-06-20T10:00:00",
        summary="検証してください" * 12,  # ~96 cells uncut
    )
    rendered, _flat, _ys = render_pending([view])
    plain = _plain(rendered)
    sum_lines = [ln for ln in plain.split("\n") if "検証してください" in ln]
    assert sum_lines, f"summary line not found. Rendered:\n{plain}"
    width = cell_len(sum_lines[0])
    assert width <= 70, (
        f"pending summary line overflows ({width} cells, codepoint-slice ≈126): "
        f"{sum_lines[0]!r}"
    )
    assert "…" in sum_lines[0], (
        f"long CJK summary should be truncated with an ellipsis: {sum_lines[0]!r}"
    )
