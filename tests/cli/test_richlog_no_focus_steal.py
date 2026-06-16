"""Tier 2: conv-pane RichLog does not steal focus from the input bar.

RichLog inherits ``can_focus = True`` from Textual's ``ScrollView``, so
the default behaviour is to gain focus on click (or via Shift+Tab from
the input bar). Once that happens the log absorbs no typed input — the
user types their next message into a dead widget. This bug was visible
in two separate paths in the UX audit:

  • Mouse click on a previous reply silently kills typing.
  • Shift+Tab from the input bar lands in RichLog, silently kills typing.

The fix sets ``can_focus = False`` on the RichLog instance. Turn
navigation (Ctrl+P/N) calls ``log.scroll_to`` without needing focus, so
disabling focus loses no real capability. Both bug paths reduce to
"focus stays on InputBar after the action".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog, TextArea

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView, InputBar


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_conv_richlog_is_not_focusable() -> None:
    """Tier 2b: The conv-pane RichLog must declare ``can_focus = False``.

    Pinned at the instance level (not the class level — the right-panel
    preview RichLog stays focusable for j/k scrolling).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one("#log", RichLog)
        assert log.can_focus is False, (
            f"conv RichLog must not be focusable; got can_focus={log.can_focus}"
        )


@pytest.mark.asyncio
async def test_clicking_conv_pane_does_not_steal_focus_from_input() -> None:
    """Tier 2b: Mouse click on the conv pane keeps focus on the input TextArea."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        # Confirm starting state: TextArea has focus
        ta = app.query_one("#input", TextArea)
        assert app.focused is ta, (
            f"input area should have initial focus; got {app.focused}"
        )

        # Simulate click on the conv pane
        await pilot.click("#conversation")
        await pilot.pause()

        # Focus must NOT have moved into the RichLog
        log = app.query_one("#log", RichLog)
        assert app.focused is not log, (
            "RichLog stole focus on click — this is the bug the fix addresses"
        )


@pytest.mark.asyncio
async def test_shift_tab_from_input_does_not_land_in_richlog() -> None:
    """Tier 2b: Shift+Tab from the input bar must not focus the conv RichLog.

    With ``can_focus = False``, Textual's DOM walker skips the RichLog and
    either wraps around or lands on the next focusable peer. Either way
    the user's typing remains routable.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("shift+tab")
        await pilot.pause()

        log = app.query_one("#log", RichLog)
        assert app.focused is not log, (
            f"Shift+Tab landed on RichLog; got focus on {app.focused}"
        )


@pytest.mark.asyncio
async def test_ctrl_p_turn_nav_still_scrolls_without_focus() -> None:
    """Tier 2b: Ctrl+P/N turn-nav must continue to work even though RichLog can't focus.

    Defensive check: the existing nav path uses ``log.scroll_to`` which
    operates on the widget directly rather than via the focused widget,
    so disabling focus is invisible to the nav. Pins this so a future
    refactor of the nav doesn't reintroduce a focus dependency.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Lay down two turn headers so anchors exist
        conv._maybe_write_header("user", ">", "bold")
        conv._maybe_write_header("reyn", "⏺", "bold")
        await pilot.pause()
        assert len(conv.turn_anchors_snapshot()) >= 1

        # jump_prev_turn must not raise even without RichLog focus
        conv.jump_prev_turn()
        conv.jump_next_turn()
        await pilot.pause()
        # Focus stays on the input area
        ta = app.query_one("#input", TextArea)
        assert app.focused is ta or app.focused is None, (
            f"focus should not have leaked off the input; got {app.focused}"
        )
