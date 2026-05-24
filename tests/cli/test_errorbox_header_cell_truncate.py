"""Tier 2: ErrorBox header truncates by cells, not code points (I-F4).

Wave-10 follow-up Topic I finding F4 (P2): ``_header_text`` used
``len(first_line) > 72`` and ``first_line[:71]`` to gate / build
the truncated header. ``len()`` counts code points; the header is
rendered in a 1-line ``Label`` whose visual budget is in terminal
cells. CJK / emoji consume 2 cells per character, so a 72-code-
point CJK message is ~144 cells — far past the typical conv-pane
width — and silently wraps to a second line, breaking the
``height: 1`` CSS contract.

After the fix the budget guard uses ``rich.cells.cell_len`` and
the body is built char-by-char with a per-char cell-width counter,
matching the sticky_status / events_tab truncation idiom.

Public surfaces tested:
  - ASCII header within 72 chars → unchanged (regression guard)
  - ASCII header over 72 chars → truncated with ``…`` (regression
    guard for the existing path)
  - CJK header whose cell-width exceeds 72 → truncated
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _header(message: str) -> str:
    """Build an ErrorBox header line for ``message`` (no markup; raw text).

    Constructs the widget bypassing super().__init__ so the test
    doesn't need the full Textual harness for what is a pure-string
    formatting check.
    """
    from reyn.chat.tui.widgets.error_box import ErrorBox
    box = ErrorBox.__new__(ErrorBox)
    box._skill_name = ""
    box._run_id_short = ""
    box._first_line_for_header = message
    box._expanded = False
    # ``_header_text`` reads ``_first_line_for_header`` + ``_expanded``
    # + ``_prefix()`` which reads ``_skill_name`` / ``_run_id_short``.
    return box._header_text()


def test_short_ascii_header_unchanged() -> None:
    """Tier 2b: a 50-cell ASCII message is rendered verbatim (regression guard)."""
    msg = "short error happened in file_read op"
    out = _header(msg)
    assert msg in out
    assert "…" not in out


def test_long_ascii_header_truncated_with_ellipsis() -> None:
    """Tier 2b: >72 code-point ASCII still truncates with ellipsis (regression guard)."""
    msg = "a" * 200
    out = _header(msg)
    assert "…" in out


def test_cjk_header_truncated_to_cell_budget() -> None:
    """Tier 2b: CJK / wide-char header is bounded by cell width.

    Pre-fix a 50-char CJK message (= 100 cells) passed the
    ``len() > 72`` guard untruncated. The label then wrapped to a
    second line, violating the ``height: 1`` CSS contract.
    """
    # 50 CJK chars × 2 cells = 100 cells; pre-fix this would NOT
    # be truncated by len()>72 (=50 < 72), so it passed through
    # untouched and wrapped.
    msg = "日本語のエラーメッセージ" * 5  # ~60 chars / 120 cells
    out = _header(msg)
    # Must be truncated now.
    assert "…" in out
