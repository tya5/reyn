"""Tier 2: /copy ring buffer holds the last N agent replies addressable by index.

Before this fix ``ConversationView`` stored only the latest agent reply
in a single slot, so users who wanted to grab the reply from one turn
ago had no path — drag-select is blocked by Textual mouse-capture and
``/copy`` always returned the most recent text. This pins the new
contract:

  1. ``_recent_replies`` is a bounded ring (≤ ``_RECENT_REPLIES_MAX``).
  2. ``last_reply_text()`` still returns the newest reply (backwards compat
     for the existing TUI handler and any external consumers).
  3. ``reply_at(n)`` is 1-indexed: n=1 = newest, n=2 = one before, etc.
  4. ``reply_at(n)`` returns None for out-of-range (≤ 0 or beyond buffered).
  5. ``recent_reply_count()`` is the buffer depth.

Plus end-to-end smoke that the slash command routes the optional index
arg through the outbox sentinel to the TUI handler.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView
from reyn.tui.widgets.conversation import _RECENT_REPLIES_MAX


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_ring_starts_empty() -> None:
    """Tier 2b: No replies yet → accessors return None / 0."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv.last_reply_text() is None
        assert conv.reply_at(1) is None
        assert conv.recent_reply_count() == 0


@pytest.mark.asyncio
async def test_recent_replies_addressable_by_one_indexed_recency() -> None:
    """Tier 2b: Three replies → reply_at(1) newest, reply_at(2) second-newest, …."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown("reply A")
        conv._write_agent_markdown("reply B")
        conv._write_agent_markdown("reply C")
        await pilot.pause()

        assert conv.recent_reply_count() == 3
        assert conv.last_reply_text() == "reply C"
        assert conv.reply_at(1) == "reply C"
        assert conv.reply_at(2) == "reply B"
        assert conv.reply_at(3) == "reply A"
        # Out-of-range
        assert conv.reply_at(4) is None
        assert conv.reply_at(0) is None
        assert conv.reply_at(-1) is None


@pytest.mark.asyncio
async def test_ring_caps_at_max_drops_oldest() -> None:
    """Tier 2b: Replies past ``_RECENT_REPLIES_MAX`` push the oldest out."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Push 5 more than the cap.
        for i in range(_RECENT_REPLIES_MAX + 5):
            conv._write_agent_markdown(f"reply {i}")
        await pilot.pause()

        assert conv.recent_reply_count() == _RECENT_REPLIES_MAX
        # The newest survives; the oldest 5 are gone.
        assert conv.reply_at(1) == f"reply {_RECENT_REPLIES_MAX + 4}"
        assert conv.reply_at(_RECENT_REPLIES_MAX) == "reply 5"
        # Stuff that fell off
        assert conv.reply_at(_RECENT_REPLIES_MAX + 1) is None


@pytest.mark.asyncio
async def test_last_reply_text_returns_newest_for_back_compat() -> None:
    """Tier 2b: Backwards-compat: ``last_reply_text()`` is still the newest reply.

    External consumers (existing TUI handler, future slash commands)
    must keep working without a per-call N argument when they just want
    "the most recent thing".
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._write_agent_markdown("first")
        conv._write_agent_markdown("second")
        await pilot.pause()
        assert conv.last_reply_text() == "second"


def test_slash_copy_forwards_arg_to_outbox_sentinel() -> None:
    """Tier 1: ``/copy 2`` produces an outbox sentinel carrying ``"2"`` in ``text``.

    Decouples the slash layer from the parsing logic that lives in the
    outbox handler — slash just forwards the trimmed arg.
    """
    import asyncio

    from reyn.chat.outbox import OutboxMessage
    from reyn.slash.copy import copy_cmd

    sent: list[OutboxMessage] = []

    class _FakeSession:
        async def _put_outbox(self, msg: OutboxMessage) -> None:
            sent.append(msg)

    asyncio.run(copy_cmd(_FakeSession(), "  2  "))
    asyncio.run(copy_cmd(_FakeSession(), "list"))
    asyncio.run(copy_cmd(_FakeSession(), ""))

    assert all(m.kind == "__copy_last_reply__" for m in sent)
    assert sent[0].text == "2"        # whitespace trimmed
    assert sent[1].text == "list"
    assert sent[2].text == ""         # empty → handler picks "latest"
