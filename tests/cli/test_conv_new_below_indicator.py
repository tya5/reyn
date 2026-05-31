"""Tier 2: conv pane "↓ N new" affordance while the user is scrolled up.

Issue #1144 #1. The conv pane already suppresses auto-scroll when the user
scrolls up to read history (``_user_scrolled`` + ``auto_scroll`` flip — see
test_user_scroll_suppresses_auto.py). But until now nothing TOLD the user
that new content had landed below their locked viewport — they had to blind-
guess Alt+End. This adds a docked ``↓ N new lines below · Alt+End`` strip,
shown only while scrolled up AND content arrived since the lock.

Contract pinned here (public surface only — ``conv.new_below_count`` accessor
+ the ``#new-below`` strip's ``hidden`` class; no private-state asserts):
  - following the tail (not scrolled up) → count stays 0 even as content writes
  - scrolled up + later writes → count > 0 (the affordance appears)
  - returning to the bottom → count resets to 0 (affordance clears)
  - the strip's ``hidden`` class tracks count (0 ⇒ hidden, >0 ⇒ visible)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text
from textual.widgets import RichLog, Static

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _fill_log(log: RichLog, n_lines: int, *, label: str = "baseline") -> None:
    """Lay down enough content to make ``max_scroll_y`` non-zero."""
    for i in range(n_lines):
        log.write(Text(f"{label} {i}"))


@pytest.mark.asyncio
async def test_following_tail_keeps_count_zero() -> None:
    """Tier 2: while following the tail, writes never raise the new-below count."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        await pilot.pause()
        # Never scrolled up → following the tail → no "new below" affordance.
        assert conv.user_scrolled is False
        assert conv.new_below_count == 0


@pytest.mark.asyncio
async def test_scrolled_up_then_content_raises_count() -> None:
    """Tier 2: content arriving after a scroll-up lock shows the ``↓ N new`` strip."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        assert log.max_scroll_y > 5, (
            f"test setup: need scrollback (max_scroll_y={log.max_scroll_y})"
        )

        # User scrolls up → lock the view, baseline captured, nothing new yet.
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True
        assert conv.new_below_count == 0, "no new content has arrived since lock yet"

        # New content lands below the locked viewport.
        _fill_log(log, 20, label="late")
        await pilot.pause()
        await pilot.pause()
        assert conv.new_below_count > 0, (
            "content arriving while scrolled up must raise the new-below count"
        )

        # The strip is visible (not hidden) when the count is positive.
        strip = conv.query_one("#new-below", Static)
        assert not strip.has_class("hidden")


@pytest.mark.asyncio
async def test_return_to_bottom_clears_count() -> None:
    """Tier 2: scrolling back to the tail clears the count + hides the strip."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        _fill_log(log, 20, label="late")
        await pilot.pause()
        await pilot.pause()
        assert conv.new_below_count > 0  # precondition: affordance is showing

        # User returns to the tail.
        log.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()

        assert conv.user_scrolled is False
        assert conv.new_below_count == 0
        strip = conv.query_one("#new-below", Static)
        assert strip.has_class("hidden")


@pytest.mark.asyncio
async def test_strip_starts_hidden() -> None:
    """Tier 2: the strip is hidden on a fresh pane (default following state)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        strip = conv.query_one("#new-below", Static)
        assert strip.has_class("hidden")
        assert conv.new_below_count == 0
