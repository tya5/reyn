"""Tier 2: ReynHeader clock renders as ``HH:MM:SS`` (8 cells), not full date.

The previous ``_now_text`` returned ``%Y-%m-%d %H:%M:%S`` (19 cells)
which pushed the right-side status (agent · model · tokens · cost ·
clock) past the 80-col terminal boundary — the seconds portion (= the
"is the UI frozen?" canary) was the first thing to clip out of view.

Compact to ``%H:%M:%S`` (8 cells) saves 11 cells per render, restoring
the canary's visibility at the cold-default 80-col width. Date context
is still surfaced once in the conv pane via ``_date_separator`` so no
information is lost.

Contract pinned:

1. ``_now_text()`` returns a string matching ``^\\d{2}:\\d{2}:\\d{2}$``
   (= H, M, S all 2-digit, single-colon-separated).
2. The string is ≤ 8 cells (= no date prefix slipped back in).
3. ``_format_status()`` includes the bare clock text without leading
   year/month characters.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from rich.text import Text as RichText

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.header import ReynHeader


def test_now_text_is_hh_mm_ss_only() -> None:
    """Tier 2: ``_now_text`` returns the 8-cell ``HH:MM:SS`` shape."""
    out = ReynHeader._now_text()
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", out), (
        f"_now_text must be HH:MM:SS only; got {out!r}"
    )


def test_now_text_fits_8_cells() -> None:
    """Tier 2: clock width is exactly 8 cells (= bounded for header layout)."""
    out = ReynHeader._now_text()
    cells = RichText(out).cell_len
    assert cells == 8, (
        f"clock must be 8 cells (HH:MM:SS); got {cells} from {out!r}"
    )


def test_format_status_contains_compact_clock_not_full_date() -> None:
    """Tier 2: ``_format_status`` surfaces ``HH:MM:SS`` and NOT ``YYYY-``.

    Defends against a refactor that re-introduces a full-date format in
    a different helper (e.g. ``_now_text_long``) and accidentally wires
    it into the header again.
    """
    header = ReynHeader(agent_name="test", model="test")
    status = header._format_status()
    plain = str(status)
    # No year prefix in the visible status text.
    assert "20" not in plain or " 20" not in plain, (
        f"clock must not include a year-shaped prefix; got {plain!r}"
    )
    # The compact clock pattern should be findable.
    assert re.search(r"\d{2}:\d{2}:\d{2}", plain), (
        f"compact HH:MM:SS clock missing from status; got {plain!r}"
    )
