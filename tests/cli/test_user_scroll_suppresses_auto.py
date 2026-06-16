"""Tier 2: user scroll-up suppresses auto-scroll, snap-back on re-engage.

Async-event UX audit (HIGH severity Finding F2): ``_user_scrolled`` was
a dead flag — declared but never set. ``RichLog.auto_scroll`` defaulted
to True, so every ``log.write(...)`` snapped the view back to the
bottom. A user reading old history would be ripped away on every
stream chunk, trace, or status line.

The fix:
  1. ``ConversationView.on_mount`` attaches a reactive watcher on the
     RichLog's ``scroll_y``.
  2. When ``scroll_y`` drops below ``max_scroll_y - 1``, ``auto_scroll``
     flips to False and ``_user_scrolled`` flips to True.
  3. When the user returns to (within 1 cell of) the bottom,
     ``auto_scroll`` is re-armed and the flag clears.
  4. ``render_user_message`` and ``clear`` explicitly snap back to the
     bottom — those are "I'm re-engaging" actions and shouldn't keep
     the user pinned to old content.

These tests pin all three transitions plus the snap-back paths.
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

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _fill_log(log: RichLog, n_lines: int) -> None:
    """Lay down enough content to make ``max_scroll_y`` non-zero."""
    for i in range(n_lines):
        log.write(Text(f"baseline {i}"))


# ── starting state ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state_auto_scroll_on_flag_off() -> None:
    """Tier 2: on mount, ``auto_scroll`` is True and ``_user_scrolled`` is False."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        assert log.auto_scroll is True
        assert conv.user_scrolled is False


# ── user scroll up → suppress ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scroll_up_disables_auto_scroll_and_sets_flag() -> None:
    """Tier 2: when ``scroll_y`` drops below ``max_scroll_y - 1``, both flip.

    Drives the scroll position directly (= simulating a mouse-wheel up)
    and verifies the watcher kicks in.
    """
    app = _make_app()
    # Tall enough that scrollback exists
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        assert log.max_scroll_y > 5, (
            f"test setup: need scrollback (max_scroll_y={log.max_scroll_y})"
        )

        # Scroll up well past the threshold
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()

        assert log.auto_scroll is False, (
            "auto_scroll should flip off when user scrolls above the bottom"
        )
        assert conv.user_scrolled is True


# ── user scroll back to bottom → re-arm ──────────────────────────────────────


@pytest.mark.asyncio
async def test_returning_to_bottom_rearms_auto_scroll() -> None:
    """Tier 2: scrolling back to the tail re-enables ``auto_scroll``."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()

        # User scrolls up
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert log.auto_scroll is False
        assert conv.user_scrolled is True

        # User scrolls back to the bottom
        log.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()

        assert log.auto_scroll is True, (
            "auto_scroll should re-arm when user returns to the tail"
        )
        assert conv.user_scrolled is False


# ── writes during scroll-up don't snap back ──────────────────────────────────


@pytest.mark.asyncio
async def test_write_during_user_scroll_preserves_position() -> None:
    """Tier 2: ``log.write`` while user is scrolled up doesn't rip them away.

    This is the user-visible behaviour the audit flagged: the dead
    ``_user_scrolled`` flag meant every write snapped the view to the
    bottom. With the fix, ``auto_scroll = False`` keeps the view where
    the user put it.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()

        # User scrolls up
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        scroll_before = log.scroll_y
        assert scroll_before == 0
        assert log.auto_scroll is False

        # A new write arrives (= async outbox event)
        log.write(Text("late-arriving stream chunk"))
        await pilot.pause()

        # User's scroll position must be preserved
        assert log.scroll_y == scroll_before, (
            f"write while user scrolled up changed scroll_y: "
            f"before={scroll_before}, after={log.scroll_y}"
        )


# ── snap-back on user submit / clear ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_user_message_snaps_back_to_bottom() -> None:
    """Tier 2: submitting a new message re-engages the user and snaps to tail.

    A user who scrolled up to read history might submit a new message
    while still scrolled up. The submit is an "I'm back" signal — we
    snap to the bottom so they see their input land and the agent reply
    track the tail.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert log.auto_scroll is False
        assert conv.user_scrolled is True

        conv.render_user_message("hello again")
        await pilot.pause()
        await pilot.pause()

        assert log.auto_scroll is True
        assert conv.user_scrolled is False
        # And we're (close to) the bottom — scroll_y == max_scroll_y
        assert log.scroll_y >= log.max_scroll_y - 1


@pytest.mark.asyncio
async def test_clear_resets_scroll_state() -> None:
    """Tier 2: ``clear()`` puts the user back at a fresh blank log.

    Any prior scroll-up state is meaningless once the content it was
    reading is gone, so we re-arm ``auto_scroll`` and clear the flag.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert conv.user_scrolled is True
        assert log.auto_scroll is False

        conv.clear()
        await pilot.pause()
        assert conv.user_scrolled is False
        assert log.auto_scroll is True
