"""Tier 2: StreamingRow scrolls itself into view as content grows (F-F11).

Wave-9 Topic F finding F11 (P1): StreamingRow mounts as a sibling
of the RichLog (not as a log line), and ``RichLog.auto_scroll``
only tracks the log's own lines. As tokens wrap and the row grows,
the bottom of the row slips below the viewport on a short
conversation — the user must manually scroll to see new tokens
arrive, defeating the purpose of streaming UX.

``_flush_render`` now calls ``self.scroll_visible(animate=False)``
when the row's content actually grew (= ``_height_dirty`` set by
``append``). Cursor-blink ticks that re-render but don't change
height are gated out so we don't fire 60 redundant scroll calls
per second during idle.

Public surfaces tested:
  - ``append()`` sets ``_height_dirty`` (= the next flush will scroll)
  - ``_flush_render`` calls ``scroll_visible`` once per append cycle
  - cursor-blink rerenders without a preceding ``append`` do NOT call
    ``scroll_visible`` (regression guard against the 60 Hz spam path)
  - ``scroll_visible`` exceptions are swallowed (= the row keeps
    rendering even when no scrollable ancestor exists)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_append_marks_height_dirty() -> None:
    """Tier 2: ``append()`` flips ``_height_dirty`` so the next flush scrolls."""
    from reyn.interfaces.tui.widgets.streaming_row import StreamingRow

    row = StreamingRow(prefix="")
    assert row.height_dirty is False
    row.append("hello")
    assert row.height_dirty is True


@pytest.mark.asyncio
async def test_flush_render_calls_scroll_visible_once_per_append_cycle() -> None:
    """Tier 2: scroll_visible is invoked exactly once per append + flush."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("scroll-test-id", "test-agent")
        await pilot.pause()

        calls: list[None] = []
        original = row.scroll_visible

        def _spy(**kwargs) -> None:  # type: ignore[no-untyped-def]
            calls.append(None)
            return original(**kwargs)

        row.scroll_visible = _spy  # type: ignore[method-assign]

        # Reset (the row may have flushed during begin_stream).
        calls.clear()
        row.height_dirty = False

        row.append("hello world")
        assert row.height_dirty is True
        row._flush_render()
        (only_call,) = calls  # exactly one scroll_visible per append+flush cycle
        # After flush, the flag is consumed.
        assert row.height_dirty is False


@pytest.mark.asyncio
async def test_cursor_blink_alone_does_not_call_scroll_visible() -> None:
    """Tier 2: idle cursor-blink rerenders don't spam scroll_visible.

    The blink path sets ``_dirty`` to retrigger the static update but
    must NOT set ``_height_dirty`` — the row's rendered height doesn't
    change when only the cursor character toggles.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("blink-test-id", "test-agent")
        await pilot.pause()

        calls: list[None] = []
        original = row.scroll_visible

        def _spy(**kwargs) -> None:  # type: ignore[no-untyped-def]
            calls.append(None)
            return original(**kwargs)

        row.scroll_visible = _spy  # type: ignore[method-assign]
        calls.clear()
        row.height_dirty = False

        # Drive enough flush ticks to cross a cursor-blink boundary
        # (= every 30 flushes, see ``_flush_render``). The row has not
        # received any ``append`` calls, so each flush is a pure blink
        # re-render.
        for _ in range(35):
            row._flush_render()
        assert calls == [], (
            f"cursor-blink-only flushes should not call scroll_visible, "
            f"got {len(calls)} calls"
        )


@pytest.mark.asyncio
async def test_scroll_visible_exception_is_swallowed() -> None:
    """Tier 2: a ``scroll_visible`` exception does not break the flush.

    If the row has no scrollable ancestor (= rare; pre-mount in some
    test harnesses), the call may raise. The bare ``try / except``
    must let the static update path proceed.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("err-test-id", "test-agent")
        await pilot.pause()

        def _explode(**kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("no scrollable ancestor")

        row.scroll_visible = _explode  # type: ignore[method-assign]

        row.append("payload")
        # Must not raise.
        row._flush_render()
        # And the underlying text was still committed to the Static
        # (= rendering path completed).
        assert "payload" in row.full_text()
