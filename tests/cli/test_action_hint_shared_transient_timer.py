"""Tier 2: F-key action status hints share the single transient auto-hide handle.

The F-key hints (F3 expand tip / "no active rows", F4 "no active tasks",
F7 "no recent tool failure" / "row flushed", F9 "timestamps: …") armed a RAW
``self.set_timer(N, conv.hide_status)`` instead of going through the
``OutboxRouter`` single-handle seam (``_show_transient_status`` /
``_cancel_transient_timer``) that the outbox transients (``/copy``,
``/cost-inline``, …) use.

Consequences of the separate, untracked timer:
  • A live status / agent reply taking over the sticky (``_on_status`` /
    ``_on_stream_start``) cancels the ROUTER's handle — but NOT a stale
    action-hint timer. So a hint armed just before a turn started would fire
    ~N s in and hide the live ``⟳ thinking…`` indicator (= the exact Async-F4
    race the router seam fixed, left open on the keypress path).
  • Two transients from different sources (an outbox ``/copy`` + an F-key hint)
    each kept their own timer; the older one hid the newer sticky.

Fix: action hints route through the same single transient handle, so any
load-bearing takeover (live status, stream start, attach) or a newer transient
cancels them too.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.app_outbox import OutboxRouter
from reyn.interfaces.tui.widgets import ConversationView, ReynHeader
from reyn.interfaces.tui.widgets.sticky_status import StickyStatus
from reyn.runtime.outbox import OutboxMessage


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="aria", model="test-model", budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_action_hint_arms_shared_transient_handle() -> None:
    """Tier 2: an F-key hint arms the router's single transient timer handle."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        # Simulate the drain being active so the shared router seam exists.
        router = OutboxRouter(app)
        app._outbox_router = router

        # F4 with an empty async strip → "no active tasks in async strip" hint.
        app.action_focus_async_stack()
        await pilot.pause()

        assert router.has_transient_status_timer(), (
            "an F-key action hint must arm the SHARED transient handle "
            "(not a private untracked set_timer)"
        )


@pytest.mark.asyncio
async def test_live_status_cancels_stale_action_hint_timer() -> None:
    """Tier 2: a live status takeover cancels a pending action-hint timer.

    Otherwise the stale hint timer fires mid-turn and hides the live
    ``thinking…`` indicator (Async-F4 race on the keypress path).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)
        app._outbox_router = router

        # Arm an action hint (F4, empty strip).
        app.action_focus_async_stack()
        await pilot.pause()
        assert router.has_transient_status_timer()

        # A live status arrives — the takeover must cancel the hint's timer.
        router._on_status(
            OutboxMessage(kind="status", text="thinking…"), conv, header,
        )
        assert not router.has_transient_status_timer(), (
            "a live-status takeover must cancel the pending action-hint timer "
            "so it cannot later hide the live indicator"
        )
        assert conv.query_one("#sticky-status", StickyStatus).has_class("active"), (
            "the live status must be showing after the takeover"
        )
