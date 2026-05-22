"""Tier 2: ConversationView.clear() empties _recent_replies (G-F3).

Wave-10 Topic G finding F3 (P2): ``clear()`` reset
``_turn_anchors``, ``_last_long_reply``, header-grouping state, etc.
but never touched ``_recent_replies``. After Ctrl+L, ``/copy`` /
``/copy N`` returned agent replies from the now-invisible prior
session — confusing the user (= "where did this content come
from?") and potentially surfacing content they had intentionally
cleared.

``clear()`` now also calls ``self._recent_replies.clear()``,
matching the lifecycle of ``_last_long_reply`` directly above.
The fresh-session state has zero replies in the buffer, so
``last_reply_text()`` returns ``None`` until a new agent reply is
committed.

Public surfaces tested:
  - ``last_reply_text()`` returns None after clear()
  - ``reply_at(1)`` returns None after clear()
  - ``recent_reply_count()`` is 0 after clear()
  - the prior reply survives in the buffer BEFORE clear (= regression
    guard that we didn't accidentally start clearing too early)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_clear_empties_recent_replies_buffer() -> None:
    """Tier 2: after clear() ``/copy`` finds no prior reply."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Simulate two agent replies committed before clear.
        conv._write_agent_markdown_with_fold("first reply text")
        conv._write_agent_markdown_with_fold("second reply text")
        await pilot.pause()

        # Sanity: the buffer has both before clear.
        assert conv.recent_reply_count() == 2
        assert conv.last_reply_text() == "second reply text"
        assert conv.reply_at(2) == "first reply text"

        conv.clear()
        await pilot.pause()

        # Post-clear: empty.
        assert conv.recent_reply_count() == 0
        assert conv.last_reply_text() is None
        assert conv.reply_at(1) is None
        assert conv.reply_at(2) is None


@pytest.mark.asyncio
async def test_new_reply_after_clear_starts_fresh_buffer() -> None:
    """Tier 2: post-clear replies populate a fresh buffer (regression guard).

    Ensures clear() doesn't break the natural append path — a new
    reply lands at index 1 and is reachable via ``last_reply_text``.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown_with_fold("pre-clear reply")
        await pilot.pause()
        conv.clear()
        await pilot.pause()
        conv._write_agent_markdown_with_fold("post-clear fresh reply")
        await pilot.pause()
        assert conv.recent_reply_count() == 1
        assert conv.last_reply_text() == "post-clear fresh reply"
        # The pre-clear reply must NOT leak into the new buffer.
        assert conv.reply_at(2) is None
