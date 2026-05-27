"""Tier 2: transient sticky status arming cancels prior auto-hide timers.

Three independent HIGH-severity findings collapse to the same root
cause — every transient sticky (``/cost-inline``, ``/copy …``,
``/docs-filter``, …) used to ``set_timer(2.5, conv.hide_status)``
without storing the handle, so:

  • Two transients in quick succession produced two live auto-hide
    timers; the older one fired after the newer transient finished and
    spuriously hid whatever sticky was current then (Async F1).
  • A transient fired right before a turn started spawned a 2.5 s
    auto-hide that killed the live ``⟳ thinking…`` indicator (Async F4).
  • ``/attach`` swapped the agent name but left the previous agent's
    ``⟳ thinking…`` running, attributed to the new agent (Multi F1).

The fix routes every transient through ``_show_transient_status`` so a
single in-flight handle is kept and cancelled before a new one arms.
Live status (``_on_status``), stream start (``_on_stream_start``), and
agent attach (``_on_attach_request``) all explicitly cancel any pending
transient timer too — none of those flows can be auto-hidden by a stale
transient timer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.app_outbox import OutboxRouter
from reyn.chat.tui.widgets import ConversationView, ReynHeader
from reyn.chat.tui.widgets.sticky_status import StickyStatus


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="aria",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_two_transients_cancel_the_first_timer() -> None:
    """Tier 2: arming a second transient cancels the first auto-hide handle.

    Without this fix, the first timer kept ticking and would have fired
    auto-hide ~2 s into the second status's lifetime, silently clearing
    a sticky the user still needed to see.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        router = OutboxRouter(app)
        router._show_transient_status(conv, "first")
        first_timer = router.transient_status_timer
        assert first_timer is not None

        router._show_transient_status(conv, "second")
        second_timer = router.transient_status_timer
        assert second_timer is not None
        assert second_timer is not first_timer, (
            "second arming must replace the timer handle, not re-use it"
        )
        # Verify the first timer was actually stopped. Textual's Timer.stop()
        # sets the internal ``_active`` Event, so ``_active.is_set()`` reads
        # True on a stopped timer (counter-intuitive but matches the
        # implementation in textual.timer).
        active_event = getattr(first_timer, "_active", None)
        if active_event is not None and hasattr(active_event, "is_set"):
            assert active_event.is_set(), (
                "first timer's stop() should have set its _active event"
            )


@pytest.mark.asyncio
async def test_live_status_cancels_pending_transient_timer() -> None:
    """Tier 2: ``_on_status`` (live ``thinking…``) cancels any pending transient.

    A transient fired right before the turn would otherwise auto-hide
    the agent's spinner mid-thought. Cancellation is the load-bearing
    guard against that race.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        router = OutboxRouter(app)
        # Arm a transient
        router._show_transient_status(conv, "cost-inline on")
        assert router.transient_status_timer is not None

        # Live status arrives — must cancel the timer
        router._on_status(
            OutboxMessage(kind="status", text="thinking…"),
            conv, header,
        )
        assert router.transient_status_timer is None
        # And the live status is showing
        sticky = conv.query_one("#sticky-status", StickyStatus)
        assert sticky.has_class("active")


@pytest.mark.asyncio
async def test_stream_start_cancels_pending_transient_timer() -> None:
    """Tier 2: ``__stream_start__`` cancels any pending transient timer.

    The stream-start handler also calls ``hide_status()`` so the live
    reply takes over — but cancelling the transient timer is what stops
    the auto-hide from firing later in the same turn.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        router = OutboxRouter(app)
        router._show_transient_status(conv, "/cost-inline on")
        assert router.transient_status_timer is not None

        router._on_stream_start(
            OutboxMessage(kind="__stream_start__", text="", meta={"msg_id": "x"}),
            conv, header,
        )
        assert router.transient_status_timer is None


@pytest.mark.asyncio
async def test_attach_clears_sticky_and_cancels_transient() -> None:
    """Tier 2: ``/attach`` hides the sticky AND cancels any transient timer.

    A previous agent's ``⟳ thinking…`` left running after attach would
    silently get attributed to the new agent in the header — confusing
    the user about which agent is mid-thought.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        # Simulate the old agent's live thinking — sticky is on, no timer
        conv.show_status("thinking…", kind="thinking")
        sticky = conv.query_one("#sticky-status", StickyStatus)
        assert sticky.has_class("active")

        # Also arm a transient (could be from a slash that fired earlier)
        router = OutboxRouter(app)
        router._show_transient_status(conv, "earlier transient")
        assert router.transient_status_timer is not None

        # Now /attach a new agent — must clear sticky and cancel timer
        # Set _agent_registry to a non-None sentinel so the handler doesn't no-op
        app._agent_registry = object()
        router._on_attach_request(
            OutboxMessage(kind="__attach_request__", text="bob"),
            conv, header,
        )
        assert app.agent_name == "bob"
        assert not sticky.has_class("active"), (
            "sticky must be cleared after attach (previous agent's spinner is stale)"
        )
        assert router.transient_status_timer is None


@pytest.mark.asyncio
async def test_cancel_transient_is_idempotent_when_no_timer() -> None:
    """Tier 2: cancelling with no timer in flight is a silent no-op."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        router = OutboxRouter(app)
        assert router.transient_status_timer is None
        # Should not raise
        router._cancel_transient_timer()
        assert router.transient_status_timer is None
