"""Tier 2: fold-stash invalidation is surfaced inline, not silent.

The B3 fold mechanism stashes the full text of a long agent reply in
``_last_long_reply`` and offers `/expand` to flush the rest. The stash is
a single slot — any subsequent agent reply (short or long) overwrites
it. Before this fix the user kept seeing the old fold hint up-screen
while `/expand` silently no-ops; nothing in the log indicated that the
fold had been invalidated.

These tests pin the contract that when an earlier fold's stash is
cleared or replaced, a dim "[ ↑ earlier fold cleared ]" marker lands in
the RichLog so the user can tell `/expand` no longer points at the old
reply.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _log_text(log: RichLog) -> str:
    """Concat all strip text in the RichLog for substring searches."""
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


def _long_reply(line_count: int = 60) -> str:
    return "\n".join(f"line {i}" for i in range(line_count))


@pytest.mark.asyncio
async def test_short_reply_after_fold_writes_expired_marker():
    """Short reply following a folded long reply emits the expired marker.

    The single-slot stash is cleared by the short reply; up-screen the
    old fold hint still reads "type /expand to show", so the marker is
    the only on-screen cue that /expand no longer works.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Turn 1: long reply → stash populated, fold hint emitted
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()
        assert conv.has_pending_expand

        # Turn 2: short reply → stash cleared, marker should appear
        conv._write_agent_markdown_with_fold("short ack")
        await pilot.pause()
        assert not conv.has_pending_expand
        rendered = _log_text(log)
        assert "earlier fold cleared" in rendered, (
            f"expected expired marker after short reply, got:\n{rendered}"
        )


@pytest.mark.asyncio
async def test_long_reply_after_fold_writes_expired_marker():
    """Second long reply replaces the stash and still flags the user.

    /expand now targets the NEW reply; the marker prevents the user
    from assuming /expand still points at the older content.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()
        first_stash = conv._last_long_reply

        conv._write_agent_markdown_with_fold(_long_reply(80))
        await pilot.pause()
        # Stash was replaced (new content), not None
        assert conv._last_long_reply is not None
        assert conv._last_long_reply != first_stash
        rendered = _log_text(log)
        assert "earlier fold cleared" in rendered, (
            f"expected expired marker after replacing fold, got:\n{rendered}"
        )


@pytest.mark.asyncio
async def test_first_reply_does_not_emit_expired_marker():
    """No prior fold → no marker (the marker is only relevant on invalidation)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._write_agent_markdown_with_fold("first ever reply")
        await pilot.pause()

        rendered = _log_text(log)
        assert "earlier fold cleared" not in rendered, (
            f"marker leaked on first reply:\n{rendered}"
        )


@pytest.mark.asyncio
async def test_short_then_short_no_marker():
    """Two short replies in a row → no fold ever existed, no marker.

    Defends against accidentally emitting the marker whenever the stash
    transitions None → None.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._write_agent_markdown_with_fold("hi")
        await pilot.pause()
        conv._write_agent_markdown_with_fold("there")
        await pilot.pause()

        rendered = _log_text(log)
        assert "earlier fold cleared" not in rendered, rendered
