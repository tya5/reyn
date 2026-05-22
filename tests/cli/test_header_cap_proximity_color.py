"""Tier 2: header cost / token segments shift color near budget cap (B-F1).

Wave-8 Topic B finding F1 (P2): before this fix, the header's
``tokens`` and ``cost`` segments rendered in the default ``#aaaaaa``
regardless of how close the spend was to the configured cap. The
``[↑ budget warn: …]`` lifecycle marker fires at 80 % but the
header surface was silent until then.

Now ``_cap_proximity_color`` returns:
  - ``"#ffaa44"`` (amber) at ratio ≥ 0.75
  - ``"#ff4444"`` (red)   at ratio ≥ 0.90
  - ``None``              otherwise (= default style)

Thresholds mirror ``cost_tab._budget_bar`` for cross-surface consistency.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_cap_proximity_color_returns_none_without_cap() -> None:
    """Tier 2: no cap → no color override (= default ``#aaaaaa`` falls through)."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color(100, None) is None
    assert _cap_proximity_color(0, None) is None


def test_cap_proximity_color_returns_none_below_75_percent() -> None:
    """Tier 2: ratio < 0.75 → no color escalation."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color(0, 100) is None
    assert _cap_proximity_color(50, 100) is None
    assert _cap_proximity_color(74, 100) is None


def test_cap_proximity_color_returns_amber_between_75_and_90() -> None:
    """Tier 2: 75 % ≤ ratio < 90 % → amber."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color(75, 100) == "#ffaa44"
    assert _cap_proximity_color(80, 100) == "#ffaa44"
    assert _cap_proximity_color(89, 100) == "#ffaa44"


def test_cap_proximity_color_returns_red_at_90_or_above() -> None:
    """Tier 2: ratio ≥ 90 % → red."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color(90, 100) == "#ff4444"
    assert _cap_proximity_color(100, 100) == "#ff4444"
    assert _cap_proximity_color(150, 100) == "#ff4444"  # over cap stays red


def test_cap_proximity_color_handles_zero_cap_gracefully() -> None:
    """Tier 2: ``cap == 0`` would divide by zero → return None defensively."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color(50, 0) is None


def test_cap_proximity_color_handles_non_numeric_input() -> None:
    """Tier 2: non-numeric ``used`` / ``cap`` doesn't crash; returns None."""
    from reyn.chat.tui.widgets.header import _cap_proximity_color
    assert _cap_proximity_color("oops", 100) is None
    assert _cap_proximity_color(50, "oops") is None
    assert _cap_proximity_color(None, 100) is None
