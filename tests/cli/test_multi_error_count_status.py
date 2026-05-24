"""Tier 2: sticky status surfaces ErrorBox count when ≥ 2 stacked (C-F4).

Wave-8 Topic C finding F4 (P2): ``_MAX_VISIBLE_ERROR_BOXES = 3`` lets
up to 3 ErrorBoxes stack under the conv pane, each requiring its
own Esc to dismiss. Before this helper there was no at-a-glance
count + no clue that Esc was the dismiss key.

``_maybe_show_error_count_status`` now:
  - ≥ 2 boxes mounted → sticky reads ``"✗ N errors — Esc=1, ⇧Esc=all"``
  - 1 box mounted → sticky stays at whatever ``mount_error`` set
    (= ``"✗ error below ↓"`` for scrolled-up, or hidden at tail)
  - 0 boxes (all dismissed) → sticky hidden
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets import ConversationView  # noqa: E402


class _ConvOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield ConversationView(id="conversation")


@pytest.mark.asyncio
async def test_single_error_does_not_show_count_status() -> None:
    """Tier 2: 1 box → sticky uses single-error cue, not count."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first error")
        await pilot.pause()
        # Single-error sticky is either ``"✗ error below ↓"`` or hidden;
        # the count-form ``"N errors"`` must NOT appear.
        sticky = conv._sticky()
        if sticky is not None:
            snap = sticky.snapshot()
            assert "errors —" not in snap.get("body", "")
            assert "errors  —" not in snap.get("body", "")


@pytest.mark.asyncio
async def test_two_errors_show_count_in_sticky() -> None:
    """Tier 2: ≥ 2 boxes → sticky reads ``"✗ 2 errors — Esc=1, ⇧Esc=all"``."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first error")
        conv.mount_error(message="second error")
        await pilot.pause()
        sticky = conv._sticky()
        assert sticky is not None
        snap = sticky.snapshot()
        assert snap["active"] is True
        assert "2 errors" in snap["body"]
        assert "Esc=1" in snap["body"]


@pytest.mark.asyncio
async def test_three_errors_show_count_three() -> None:
    """Tier 2: count updates with stack depth (3 stacked boxes)."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first")
        conv.mount_error(message="second")
        conv.mount_error(message="third")
        await pilot.pause()
        sticky = conv._sticky()
        assert sticky is not None
        snap = sticky.snapshot()
        assert "3 errors" in snap["body"]


@pytest.mark.asyncio
async def test_dismiss_refreshes_count_below_threshold() -> None:
    """Tier 2: dismissing one of two boxes drops back below count threshold.

    After dismiss, ``_maybe_show_error_count_status`` should NOT keep
    the count sticky active (= n == 1 → falls through to existing
    single-error behaviour).
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first")
        conv.mount_error(message="second")
        await pilot.pause()
        # Count visible.
        sticky = conv._sticky()
        assert sticky is not None
        assert "2 errors" in sticky.snapshot()["body"]
        conv.dismiss_last_error()
        await pilot.pause()
        # One box remaining → count form should NOT linger as ACTIVE.
        # The sticky's body retains the prior text after ``hide``
        # (the widget doesn't blank it), but ``active=False`` is the
        # load-bearing visibility signal users actually see.
        snap = sticky.snapshot()
        assert snap.get("active") is False, (
            "sticky must be inactive after dismiss drops below threshold"
        )


@pytest.mark.asyncio
async def test_dismiss_all_hides_sticky() -> None:
    """Tier 2: dismissing every box → sticky cleared (= no orphan count)."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first")
        conv.mount_error(message="second")
        await pilot.pause()
        conv.dismiss_last_error()
        conv.dismiss_last_error()
        await pilot.pause()
        sticky = conv._sticky()
        assert sticky is not None
        snap = sticky.snapshot()
        assert snap.get("active") is False
