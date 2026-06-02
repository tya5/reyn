"""Tier 2: StreamingRow / InlineThinkingRow indent follows F9 timestamp-toggle state.

Fix A1: ``ConversationView._current_body_indent()`` returns 8 (ts-on) or 2
(ts-off) depending on the timestamp toggle. The RichLog body path
(``_write_body``) already uses it, but the live streaming row (StreamingRow)
and the spinner (InlineThinkingRow) were baked at fixed CSS values — 8 and 2
respectively — regardless of the actual state.

The fix passes ``_current_body_indent()`` at row-mount time so the live rows
align with surrounding body text.

Public surfaces tested:
  - ``StreamingRow.body_indent`` property (returns the indent passed at construction)
  - ``InlineThinkingRow.body_indent`` property (returns the indent passed)
  - The Static widget's ``styles.padding.left`` after mount
  - ``InlineThinkingRow.styles.padding.left`` after mount
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.conversation import (
    _BODY_INDENT_NO_TS,
    _BODY_INDENT_WITH_TS,
)

# ── StreamingRow: body_indent property mirrors the constructor arg ───────────


def test_streaming_row_body_indent_default_is_ts_on() -> None:
    """Tier 2: StreamingRow with no indent arg reports ts-on indent."""
    from reyn.chat.tui.widgets.streaming_row import BODY_INDENT_COLS, StreamingRow

    row = StreamingRow()
    assert row.body_indent == BODY_INDENT_COLS == _BODY_INDENT_WITH_TS, (
        f"default body_indent should be ts-on ({_BODY_INDENT_WITH_TS}), "
        f"got {row.body_indent}"
    )


def test_streaming_row_body_indent_custom_value() -> None:
    """Tier 2: StreamingRow(indent=N) reports N via body_indent."""
    from reyn.chat.tui.widgets.streaming_row import StreamingRow

    row = StreamingRow(indent=_BODY_INDENT_NO_TS)
    assert row.body_indent == _BODY_INDENT_NO_TS, (
        f"StreamingRow(indent={_BODY_INDENT_NO_TS}).body_indent should be "
        f"{_BODY_INDENT_NO_TS}, got {row.body_indent}"
    )


# ── StreamingRow: mounted Static padding.left == passed indent ───────────────


@pytest.mark.asyncio
async def test_begin_stream_ts_on_uses_ts_on_indent() -> None:
    """Tier 2: begin_stream while ts=on produces a row with ts-on indent."""
    from textual.widgets import Static

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv.show_timestamps is True  # default

        row = conv.begin_stream("ts-on-test", "agent")
        row.append("streaming body text")
        await pilot.pause()

        assert row.body_indent == _BODY_INDENT_WITH_TS, (
            f"ts-on: body_indent should be {_BODY_INDENT_WITH_TS}, "
            f"got {row.body_indent}"
        )
        # Also verify the Static widget got the correct padding after on_mount.
        static = row.query_one(Static)
        assert static.styles.padding.left == _BODY_INDENT_WITH_TS, (
            f"ts-on: Static padding.left should be {_BODY_INDENT_WITH_TS}, "
            f"got {static.styles.padding.left}"
        )


@pytest.mark.asyncio
async def test_begin_stream_ts_off_uses_ts_off_indent() -> None:
    """Tier 2: begin_stream while ts=off produces a row with ts-off indent.

    This is the load-bearing regression test: pre-fix the row was always
    baked at 8 even when timestamps were off (= 6-column misalignment).
    """
    from textual.widgets import Static

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Toggle timestamps off.
        conv.toggle_timestamps()
        assert conv.show_timestamps is False

        row = conv.begin_stream("ts-off-test", "agent")
        row.append("streaming body text ts-off")
        await pilot.pause()

        assert row.body_indent == _BODY_INDENT_NO_TS, (
            f"ts-off: body_indent should be {_BODY_INDENT_NO_TS}, "
            f"got {row.body_indent}"
        )
        static = row.query_one(Static)
        assert static.styles.padding.left == _BODY_INDENT_NO_TS, (
            f"ts-off: Static padding.left should be {_BODY_INDENT_NO_TS}, "
            f"got {static.styles.padding.left}"
        )


# ── InlineThinkingRow: body_indent property mirrors constructor arg ──────────


def test_inline_thinking_row_body_indent_default_is_none() -> None:
    """Tier 2: InlineThinkingRow() with no indent passes None (uses DEFAULT_CSS)."""
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    row = InlineThinkingRow()
    assert row.body_indent is None, (
        f"default body_indent should be None (use DEFAULT_CSS), got {row.body_indent}"
    )


def test_inline_thinking_row_body_indent_custom_value() -> None:
    """Tier 2: InlineThinkingRow(indent=N) reports N via body_indent."""
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    row = InlineThinkingRow(indent=_BODY_INDENT_WITH_TS)
    assert row.body_indent == _BODY_INDENT_WITH_TS, (
        f"InlineThinkingRow(indent={_BODY_INDENT_WITH_TS}).body_indent should be "
        f"{_BODY_INDENT_WITH_TS}, got {row.body_indent}"
    )


# ── InlineThinkingRow: mounted widget padding.left == passed indent ──────────


@pytest.mark.asyncio
async def test_start_thinking_ts_on_uses_ts_on_indent() -> None:
    """Tier 2: start_thinking() while ts=on mounts a row with ts-on indent."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv.show_timestamps is True

        conv.start_thinking()
        await pilot.pause()

        row = conv.query_one(InlineThinkingRow)
        assert row.body_indent == _BODY_INDENT_WITH_TS, (
            f"ts-on: InlineThinkingRow.body_indent should be "
            f"{_BODY_INDENT_WITH_TS}, got {row.body_indent}"
        )
        assert row.styles.padding.left == _BODY_INDENT_WITH_TS, (
            f"ts-on: InlineThinkingRow padding.left should be "
            f"{_BODY_INDENT_WITH_TS}, got {row.styles.padding.left}"
        )


@pytest.mark.asyncio
async def test_start_thinking_ts_off_uses_ts_off_indent() -> None:
    """Tier 2: start_thinking() while ts=off mounts a row with ts-off indent.

    Pre-fix the row used DEFAULT_CSS padding 0 2 (= ts-off = 2) regardless
    of state, so ts-on was wrong at 2 (body at 8, spinner at 2 = 6 cols left
    of body).
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Toggle off.
        conv.toggle_timestamps()
        assert conv.show_timestamps is False

        conv.start_thinking()
        await pilot.pause()

        row = conv.query_one(InlineThinkingRow)
        assert row.body_indent == _BODY_INDENT_NO_TS, (
            f"ts-off: InlineThinkingRow.body_indent should be "
            f"{_BODY_INDENT_NO_TS}, got {row.body_indent}"
        )
        assert row.styles.padding.left == _BODY_INDENT_NO_TS, (
            f"ts-off: InlineThinkingRow padding.left should be "
            f"{_BODY_INDENT_NO_TS}, got {row.styles.padding.left}"
        )
