"""Tier 2: a priority-suppressed transient must not arm a hide timer.

`StickyStatus.show()` suppresses a lower-priority transient when a
higher-priority sticky (error / mode / terminal-error) is active — so a routine
`/copy` "copied" toast can't overwrite a "CRITICAL: auth failed" error. But
`OutboxRouter._show_transient_status` armed its auto-hide timer
UNCONDITIONALLY, even when the show was suppressed. The timer then fired and
hid the higher-priority incumbent the suppression had just protected — the
exact Wave-10 G-F8/I-F8 failure, reintroduced via the hide path.

Fix: `show()` reports whether it displayed; `_show_transient_status` arms the
hide timer only when the transient actually took over the sticky.
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
from reyn.interfaces.tui.widgets import ConversationView
from reyn.interfaces.tui.widgets.sticky_status import StickyStatus


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="t", model="m", budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_suppressed_transient_does_not_hide_active_error() -> None:
    """Tier 2: a suppressed general transient must not arm a hide timer that
    later dismisses the higher-priority error it could not overwrite."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        router = OutboxRouter(app)

        # An error is showing (priority 80).
        conv.show_status("CRITICAL: auth failed", kind="error")
        assert sticky.snapshot()["active"] is True

        # A routine transient (priority 50) — suppressed by the error, but
        # must NOT arm a hide timer.
        router._show_transient_status(conv, "copied reply", duration=0.2)
        snap_after = sticky.snapshot()
        assert snap_after["body"] == "CRITICAL: auth failed", (
            "the error must still be the displayed body (transient suppressed)"
        )
        assert router.has_transient_status_timer() is False, (
            "a suppressed transient must not arm a hide timer"
        )

        # Past the transient's would-be hide deadline — the error must survive.
        await pilot.pause(0.3)
        survived = sticky.snapshot()
        assert survived["active"] is True, (
            "the higher-priority error must survive a suppressed transient's "
            "(non-)timer"
        )
        assert survived["body"] == "CRITICAL: auth failed"


@pytest.mark.asyncio
async def test_unsuppressed_transient_still_arms_and_hides() -> None:
    """Tier 2: a transient that DOES display still auto-hides (no regression)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        router = OutboxRouter(app)

        # No incumbent → the transient displays and arms its hide timer.
        router._show_transient_status(conv, "copied reply", duration=0.2)
        shown = sticky.snapshot()
        assert shown["active"] is True
        assert shown["body"] == "copied reply"
        assert router.has_transient_status_timer() is True

        await pilot.pause(0.3)
        assert sticky.snapshot()["active"] is False, (
            "an un-suppressed transient must still auto-hide after its duration"
        )
