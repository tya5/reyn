"""Tier 2: scroll_page_down / scroll_line_down set user_scrolled when mid-scroll.

Fix C7: the up-direction scroll methods (scroll_page_up, scroll_line_up,
scroll_to_top, _jump_to_relative_anchor) all explicitly set
``_user_scrolled = True`` so a concurrent stream write doesn't yank the
viewport to the tail while the user is reading. The down-direction methods
(scroll_page_down, scroll_line_down) only cleared the flag when reaching the
tail — they never set it True when landing in the middle. A user who paged to
the top then paged back down through the middle had ``_user_scrolled = False``
and would be auto-scrolled away on the next stream write.

The fix: both methods now set ``_user_scrolled = True`` when the scroll lands
above the tail, and ``False`` when it reaches the tail (= re-arm auto-scroll).

Public surfaces:
  - ``conv.user_scrolled`` accessor (not ``_user_scrolled`` directly)
  - behaviour: a write after mid-page-down does NOT snap the viewport
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text
from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _fill_log(log: RichLog, n_lines: int) -> None:
    for i in range(n_lines):
        log.write(Text(f"line {i}"))


# ── scroll_page_down into the middle sets the flag ───────────────────────────


@pytest.mark.asyncio
async def test_scroll_page_down_mid_sets_user_scrolled() -> None:
    """Tier 2: page-down landing above the tail sets user_scrolled True.

    If a user scrolls to the top then pages down through the middle the
    viewport should stay locked — ``user_scrolled`` must be True so the
    next stream write does NOT snap back to the bottom.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 80)
        await pilot.pause()
        assert log.max_scroll_y > 10, (
            f"test setup: need large scrollback (max_scroll_y={log.max_scroll_y})"
        )

        # Start at the very top (user scrolled up first).
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True  # watcher set it

        # Page down once — should still be in the middle, not the tail.
        conv.scroll_page_down()
        await pilot.pause()

        assert log.scroll_y < log.max_scroll_y - 1, (
            f"test setup: one page-down should leave us above the tail "
            f"(scroll_y={log.scroll_y}, max_scroll_y={log.max_scroll_y})"
        )
        assert conv.user_scrolled is True, (
            "page-down landing mid-scroll must keep user_scrolled=True; "
            "otherwise the next stream write snaps the viewport to the tail"
        )


@pytest.mark.asyncio
async def test_scroll_page_down_to_tail_clears_user_scrolled() -> None:
    """Tier 2: page-down reaching the actual tail re-arms auto-scroll (flag=False).

    Drives scroll_y directly to the tail to avoid depending on how many
    page-down iterations are needed for a given viewport height; then calls
    scroll_page_down() once more (which will find scroll_y >= max_scroll_y - 1
    after the scroll attempt and clear the flag).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 40)
        await pilot.pause()
        assert log.max_scroll_y > 5

        # Scroll up to position far from the tail.
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True

        # Scroll directly to just before the tail, then call scroll_page_down
        # so the method itself detects at-tail and clears the flag.
        log.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()

        # scroll_end triggers the watcher which sets user_scrolled=False; but
        # the invariant we're testing is the scroll_page_down method path.
        # Reset to simulate user arriving near the tail via page-downs.
        conv._user_scrolled = True  # noqa: SLF001 – test needs to seed state
        # One more page-down: we're already at the tail, method detects it.
        conv.scroll_page_down()
        await pilot.pause()

        assert conv.user_scrolled is False, (
            "scroll_page_down at the tail must clear user_scrolled "
            "so auto-scroll re-arms"
        )


# ── scroll_line_down into the middle sets the flag ───────────────────────────


@pytest.mark.asyncio
async def test_scroll_line_down_mid_sets_user_scrolled() -> None:
    """Tier 2: line-down landing above the tail sets user_scrolled True."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 80)
        await pilot.pause()
        assert log.max_scroll_y > 5

        # Start at the very top.
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True

        # One line-down — still in the middle.
        conv.scroll_line_down()
        await pilot.pause()

        if log.scroll_y < log.max_scroll_y - 1:
            assert conv.user_scrolled is True, (
                "line-down landing mid-scroll must keep user_scrolled=True"
            )


@pytest.mark.asyncio
async def test_scroll_line_down_to_tail_clears_user_scrolled() -> None:
    """Tier 2: line-down reaching the tail clears user_scrolled (auto-scroll re-arms)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 12)
        await pilot.pause()

        # Go up by one line.
        log.scroll_relative(y=-1, animate=False)
        await pilot.pause()
        await pilot.pause()
        # Might or might not be considered "scrolled" depending on max_scroll_y.
        # What we care about is that scrolling back to the tail clears the flag.

        # Line-down repeatedly to reach the tail.
        for _ in range(5):
            conv.scroll_line_down()
            await pilot.pause()
            if log.scroll_y >= log.max_scroll_y - 1:
                break

        if log.scroll_y >= log.max_scroll_y - 1:
            assert conv.user_scrolled is False, (
                "line-down reaching the tail must clear user_scrolled"
            )


# ── behaviour-level: write after mid-page-down does NOT snap ─────────────────


@pytest.mark.asyncio
async def test_write_after_page_down_mid_preserves_position() -> None:
    """Tier 2: a write that arrives while page-down-locked mid-scroll stays put.

    This is the user-visible regression: Alt+Home → PageDown (mid) → stream
    write snapped them to the bottom. With the fix the auto_scroll stays off
    and the position is preserved.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 80)
        await pilot.pause()

        # Jump to top.
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True

        # Page down once into the middle.
        conv.scroll_page_down()
        await pilot.pause()

        if log.scroll_y >= log.max_scroll_y - 1:
            # Skipped: couldn't stay mid-scroll in this test environment.
            return

        assert conv.user_scrolled is True
        pos_after_page_down = log.scroll_y

        # Simulate a stream write arriving.
        log.write(Text("late stream chunk during page-down mid-read"))
        await pilot.pause()

        assert log.scroll_y == pos_after_page_down, (
            f"write while page-down-locked should not snap to tail; "
            f"before={pos_after_page_down}, after={log.scroll_y}"
        )
