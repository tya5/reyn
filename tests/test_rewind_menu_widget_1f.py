"""Tier 2: OS invariant — RewindMenuWidget selection + scroll-window (1f).

The time-travel checkpoint picker is a passive, app-driven widget
(``can_focus = False``): the App drives navigation via ``move_selection`` and
reads the choice via ``selected_point``. These pin the selection-state surface
+ the long-list scroll window (trap 3) without needing a running Textual app.

Real RewindMenuWidget — no mocks.
"""
from __future__ import annotations

from reyn.chat.tui.widgets.rewind_menu import _MAX_VISIBLE, RewindMenuWidget


def _points(n: int) -> list[dict]:
    return [{"seq": i, "ts": f"2026-06-13T00:00:0{i}", "kind": "turn"} for i in range(n)]


def test_default_selection_is_most_recent() -> None:
    """Tier 2: selection defaults to the most-recent checkpoint (bottom row)."""
    w = RewindMenuWidget(_points(4))
    assert w.selected_index == 3
    assert w.selected_point()["seq"] == 3


def test_move_selection_clamps_not_wraps() -> None:
    """Tier 2: ↑/↓ clamp at the list bounds (finite timeline, no wrap)."""
    w = RewindMenuWidget(_points(3))  # selection starts at index 2
    w.move_selection(-1)
    assert w.selected_index == 1
    w.move_selection(-5)              # clamp at 0, no wrap to bottom
    assert w.selected_index == 0
    w.move_selection(+9)              # clamp at top, no wrap to 0
    assert w.selected_index == 2


def test_can_focus_is_false() -> None:
    """Tier 2: trap 2 — the widget never steals focus from the InputBar."""
    assert RewindMenuWidget(_points(2)).can_focus is False


def test_empty_points_safe() -> None:
    """Tier 2: empty timeline → no selection, render does not crash."""
    w = RewindMenuWidget([])
    assert w.selected_point() is None
    w.move_selection(-1)  # no-op, no crash
    assert "no checkpoints" in w.render().plain


def test_scroll_window_keeps_selection_visible() -> None:
    """Tier 2: trap 3 — with 20+ checkpoints only _MAX_VISIBLE render, windowed
    around the selection — both the selected row and the overflow markers show."""
    w = RewindMenuWidget(_points(20))  # selection starts at 19 (bottom)
    rendered = w.render().plain
    # Only a window of rows is shown, not all 20.
    shown_rows = [ln for ln in rendered.splitlines() if "#" in ln and "earlier" not in ln]
    assert len(shown_rows) <= _MAX_VISIBLE
    # The selected (bottom) row is visible; an "earlier…" overflow marker shows.
    assert "#19" in rendered
    assert "earlier" in rendered

    # Navigate to the top — the window slides; "later…" overflow now shows.
    w.move_selection(-19)
    rendered_top = w.render().plain
    assert "#0" in rendered_top
    assert "later" in rendered_top


def test_render_shows_kind_and_reltime() -> None:
    """Tier 2: each row renders the kind label + a relative-time column."""
    pts = [{"seq": 5, "ts": "2026-06-13T00:00:00", "kind": "phase"}]
    w = RewindMenuWidget(pts, rel_time_fn=lambda ts: "2m ago")
    rendered = w.render().plain
    assert "#5" in rendered
    assert "phase" in rendered
    assert "2m ago" in rendered
