"""Tier 2: end_stream_cancelled appends partial to _recent_replies (G-F10).

Wave-10 Topic G finding F10 (P2): the normal ``end_stream`` path
routed through ``_write_agent_markdown`` which appends
the reply text to ``_recent_replies``. ``end_stream_cancelled``
(= the Ctrl+C cancel path introduced by wave-9 F-F7) wrote the
partial body directly to the log, BYPASSING ``_recent_replies``.

Consequence: ``/copy`` after a cancel returned the agent reply
from TWO turns ago (= whatever was last appended via the normal
path), not the partial fragment the user just saw streaming. The
partial text was unrecoverable.

After the fix the cancelled partial is stashed in
``_recent_replies`` (capped at ``_RECENT_REPLIES_MAX``) so
``last_reply_text()`` / ``reply_at(1)`` / ``/copy`` all return the
fragment.

Public surfaces tested:
  - cancelled stream's partial text is reachable via
    ``last_reply_text()``
  - normal ``end_stream`` path still appends (regression guard)
  - empty partial is NOT appended (= ``end_stream_cancelled`` with
    no streamed body doesn't pollute the buffer)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_cancelled_partial_is_appended_to_recent_replies() -> None:
    """Tier 2: ``last_reply_text()`` returns the partial after cancel."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("cancel-recent-id", "test-agent")
        row.append("partial body the user saw streaming when they cancelled")
        await pilot.pause()

        # Pre-cancel: buffer empty (no completed reply yet).
        assert conv.recent_reply_count() == 0
        assert conv.last_reply_text() is None

        conv.end_stream_cancelled("cancel-recent-id")
        await pilot.pause()

        # Post-cancel: partial in the buffer + reachable via /copy API.
        assert conv.recent_reply_count() == 1
        assert conv.last_reply_text() == (
            "partial body the user saw streaming when they cancelled"
        )


@pytest.mark.asyncio
async def test_empty_cancel_does_not_pollute_recent_replies() -> None:
    """Tier 2: empty partial → buffer unchanged (no zero-length entry).

    Regression guard: the ``if full:`` gate at the top of the new
    append branch must prevent inserting empty strings into the
    ring buffer.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.begin_stream("empty-cancel-id", "test-agent")
        await pilot.pause()
        # Cancel without any appended content.
        conv.end_stream_cancelled("empty-cancel-id")
        await pilot.pause()
        assert conv.recent_reply_count() == 0
        assert conv.last_reply_text() is None


@pytest.mark.asyncio
async def test_cancel_then_normal_reply_both_in_buffer_order() -> None:
    """Tier 2: partial + subsequent normal reply both appear, in order.

    ``last_reply_text()`` returns the most recent (= the normal reply
    after the cancel). ``reply_at(2)`` returns the partial. Pins the
    interleaving contract.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Cancelled partial → buffer index 1.
        row = conv.begin_stream("first-cancel-id", "test-agent")
        row.append("partial fragment")
        await pilot.pause()
        conv.end_stream_cancelled("first-cancel-id")
        await pilot.pause()

        # Normal completed reply → buffer index 2.
        conv._write_agent_markdown("complete reply after cancel")
        await pilot.pause()

        assert conv.recent_reply_count() == 2
        assert conv.last_reply_text() == "complete reply after cancel"
        assert conv.reply_at(2) == "partial fragment"
