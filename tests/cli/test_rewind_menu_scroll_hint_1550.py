"""Tier 2: /rewind menu scrolled-up status-hint parity (#1550).

When the user has scrolled up to read history, the rewind picker renders at the
tail where they can't see it — so ``mount_rewind_menu`` surfaces a
``⏪ rewind menu below ↓`` cue on the StickyStatus, parity with how
``mount_intervention`` surfaces ``⚑ intervention below ↓``. At the tail the cue
is suppressed (the widget is visible). Dismissing the picker clears the cue.

Public-surface only — ``StickyStatus.snapshot()`` (active/body) + the real
mounted DOM via the ``run_test`` pilot. No private-state asserts, no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import RichLog

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView
from reyn.chat.tui.widgets._branch_tree import build_branch_tree_rows
from reyn.chat.tui.widgets.sticky_status import StickyStatus


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="test-agent", model="test-model",
        budget_tracker=None,
    )


def _tree_rows() -> list[dict]:
    """Branch-tree rows for the picker (the only mode since #1561 / flat path
    removed in #1563): one active branch + 3 checkpoints."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 3,
                 "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": i, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}
           for i in range(3)]
    return build_branch_tree_rows(branches, cps)


def _fill_log(log: RichLog, n: int) -> None:
    for i in range(n):
        log.write(Text(f"line {i}"))


@pytest.mark.asyncio
async def test_scrolled_up_mount_shows_rewind_cue() -> None:
    """Tier 2: scrolled-up + mount_rewind_menu → StickyStatus shows the
    "rewind menu below ↓" cue (parity with the intervention below-cue)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        _fill_log(log, 60)
        await pilot.pause()
        assert log.max_scroll_y > 5, "test setup: need scrollback"

        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.user_scrolled is True

        conv.mount_rewind_menu(_tree_rows())
        await pilot.pause()

        snap = sticky.snapshot()
        assert snap["active"] is True
        assert "rewind menu below" in snap["body"].lower()


@pytest.mark.asyncio
async def test_at_tail_mount_shows_no_cue() -> None:
    """Tier 2: following the tail → no below-cue (the widget is visible)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        assert conv.user_scrolled is False

        conv.mount_rewind_menu(_tree_rows())
        await pilot.pause()

        # No rewind cue while at the tail (hide_status path).
        snap = sticky.snapshot()
        assert "rewind menu below" not in snap["body"].lower()


@pytest.mark.asyncio
async def test_dismiss_clears_rewind_cue() -> None:
    """Tier 2: dismissing the picker clears the scrolled-up cue (no lingering)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        _fill_log(log, 60)
        await pilot.pause()
        log.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()

        app._rewind_menu = conv.mount_rewind_menu(_tree_rows())
        await pilot.pause()
        assert "rewind menu below" in sticky.snapshot()["body"].lower()

        app._dismiss_rewind_menu()
        await pilot.pause()
        assert sticky.snapshot()["active"] is False
