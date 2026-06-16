"""Tier 2: agents tab recent-row timestamp collapses to time-only when today.

A typical recent-skill row at default 33 % panel width was
``  ✓ direct_llm  6.5s  2026-05-19 07:15:42`` (~38 cells). At ~28
content cells, this wrapped to 4-5 lines per row and made the tab
nearly unreadable. The fix uses ``_compact_ts`` to drop the date
portion for today's runs (= recovers 11 cells per row), keeping
older runs' full ``YYYY-MM-DD HH:MM:SS`` for cross-day context.

Contract pinned:

1. Today's timestamp collapses to ``HH:MM:SS``.
2. Non-today timestamps render unchanged (= full date-time string).
3. Edge cases — empty / malformed input — pass through without
   raising.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.right_panel.agents_tab import _compact_ts


def test_today_timestamp_collapses_to_time_only() -> None:
    """Tier 2: today's ``YYYY-MM-DD HH:MM:SS`` collapses to ``HH:MM:SS``."""
    today = date.today().isoformat()
    out = _compact_ts(f"{today} 07:15:42")
    assert out == "07:15:42", out


def test_yesterday_timestamp_renders_unchanged() -> None:
    """Tier 2: non-today timestamps keep the full ``YYYY-MM-DD HH:MM:SS`` form.

    The compaction is opt-in for today only — older runs need the
    date context so the user can tell same-day from cross-day history.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    full = f"{yesterday} 07:15:42"
    assert _compact_ts(full) == full


def test_empty_string_passes_through() -> None:
    """Tier 2: edge case — empty input is returned unchanged."""
    assert _compact_ts("") == ""


def test_short_string_passes_through() -> None:
    """Tier 2: edge case — too-short strings don't crash, return as-is."""
    assert _compact_ts("foo") == "foo"
    # 9 chars — one short of the 10-char ``YYYY-MM-DD`` minimum prefix.
    assert _compact_ts("2026-05-1") == "2026-05-1"
