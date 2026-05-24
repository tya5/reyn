"""Tier 2: FoldableMarkdown replaces single-slot fold stash (B3 toggle refactor).

The old B3 fold mechanism used a single ``_last_long_reply`` slot: each
new reply (short or long) either cleared or replaced it, and a dim
"[ ↑ earlier fold cleared ]" marker was emitted to signal invalidation.

The new design mounts a ``FoldableMarkdown`` widget per long reply.
Each widget is independently toggleable, so there is no longer a
"stash invalidated" concept. The "expired marker" contract is gone.

These tests pin the new B3 toggle contract:
  - ``has_pending_expand`` reflects whether the LATEST reply was long.
  - A short follow-up clears ``has_pending_expand`` (= latest is short).
  - A second long reply adds a NEW foldable; both widgets remain mounted.
  - Short replies write nothing to the "earlier fold" path.
  - ``_last_long_reply`` transitions are still testable via public surface.
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
async def test_short_reply_after_fold_clears_has_pending_expand():
    """Tier 2: short reply after a long one clears has_pending_expand.

    FoldableMarkdown design: the new widget remains mounted for the older
    long reply; ``has_pending_expand`` tracks whether the LATEST reply
    was long (= mirrors the old slot semantics on the public API).
    """
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Turn 1: long reply → foldable mounted, has_pending_expand True
        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()
        assert conv.has_pending_expand, "long reply should set has_pending_expand"
        assert len(list(conv.query(FoldableMarkdown))) == 1

        # Turn 2: short reply → has_pending_expand False; foldable still mounted
        conv._write_agent_markdown_with_fold("short ack")
        await pilot.pause()
        assert not conv.has_pending_expand, (
            "short reply clears has_pending_expand (latest reply is short)"
        )
        # The older foldable widget is STILL mounted (not removed by short reply)
        assert len(list(conv.query(FoldableMarkdown))) == 1, (
            "older FoldableMarkdown stays mounted after a short follow-up"
        )


@pytest.mark.asyncio
async def test_long_reply_after_fold_adds_second_foldable():
    """Tier 2: second long reply mounts a second FoldableMarkdown.

    Both widgets remain independently toggleable. ``has_pending_expand``
    reflects the latest (second) reply's tail.
    """
    from reyn.chat.tui.widgets.foldable_markdown import FoldableMarkdown

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv._write_agent_markdown_with_fold(_long_reply(60))
        await pilot.pause()
        first_stash = conv._last_long_reply

        conv._write_agent_markdown_with_fold(_long_reply(80))
        await pilot.pause()
        # has_pending_expand still True (latest reply is also long)
        assert conv.has_pending_expand
        # _last_long_reply replaced with second reply's tail
        assert conv._last_long_reply is not None
        assert conv._last_long_reply != first_stash
        # Two FoldableMarkdown widgets mounted
        foldables = list(conv.query(FoldableMarkdown))
        fm_first, fm_second = foldables  # exactly 2 foldable widgets expected


@pytest.mark.asyncio
async def test_first_reply_does_not_write_expired_marker():
    """Tier 2: no expired-marker text appears for the first reply.

    The expired-marker concept is gone with FoldableMarkdown; this test
    guards against any accidental regression that re-introduces the marker.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._write_agent_markdown_with_fold("first ever reply")
        await pilot.pause()

        rendered = _log_text(log)
        assert "earlier fold cleared" not in rendered, (
            f"expired marker leaked on first reply:\n{rendered}"
        )


@pytest.mark.asyncio
async def test_short_then_short_no_expired_marker():
    """Tier 2: two short replies in a row → no expired-marker text.

    Defends against accidentally emitting the marker whenever the stash
    transitions None → None (regression from old code path).
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
