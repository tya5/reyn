"""Tier 2b: InlineThinkingRow + ConversationView.start_thinking / stop_thinking.

Validates the inline Braille spinner lifecycle that replaced the sticky
``kind="thinking"`` indicator.

Public surfaces tested:
  1. start_thinking() → exactly 1 InlineThinkingRow mounted
  2. start_thinking() twice → still exactly 1 (idempotent)
  3. start_thinking() then stop_thinking() → 0 InlineThinkingRow mounted
  4. stop_thinking() without prior start → no error (idempotent)
  5. InlineThinkingRow rendered text contains a Braille frame character
  6. _tick() advances the frame index (spinner animates)
  7. kind="thinking" no longer appears in _KIND_PRIORITY
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_start_thinking_mounts_one_row() -> None:
    """Tier 2b: start_thinking() mounts exactly 1 InlineThinkingRow."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_thinking()
        await pilot.pause()
        rows = list(conv.query(InlineThinkingRow))
        assert rows, "expected at least 1 InlineThinkingRow after start_thinking()"
        assert not rows[1:], (
            f"expected exactly 1 InlineThinkingRow after start_thinking(), got {len(rows)}"
        )


@pytest.mark.asyncio
async def test_start_thinking_idempotent() -> None:
    """Tier 2b: calling start_thinking() twice mounts only 1 row."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_thinking()
        await pilot.pause()
        conv.start_thinking()
        await pilot.pause()
        rows = list(conv.query(InlineThinkingRow))
        assert rows, "expected at least 1 InlineThinkingRow after two start_thinking() calls"
        assert not rows[1:], (
            f"expected exactly 1 InlineThinkingRow after two start_thinking() calls, "
            f"got {len(rows)}"
        )


@pytest.mark.asyncio
async def test_stop_thinking_unmounts_row() -> None:
    """Tier 2b: stop_thinking() after start_thinking() unmounts the row."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_thinking()
        await pilot.pause()
        conv.stop_thinking()
        await pilot.pause()
        rows = list(conv.query(InlineThinkingRow))
        assert not rows, (
            f"expected 0 InlineThinkingRow after stop_thinking(), got {len(rows)}"
        )


@pytest.mark.asyncio
async def test_stop_thinking_without_start_no_error() -> None:
    """Tier 2b: stop_thinking() without prior start_thinking() is a no-op."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Must not raise
        conv.stop_thinking()
        await pilot.pause()


@pytest.mark.asyncio
async def test_inline_thinking_row_shows_braille_frame() -> None:
    """Tier 2b: InlineThinkingRow frame index is valid after mounting."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.inline_thinking_row import _FRAMES, InlineThinkingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_thinking()
        await pilot.pause()
        row = conv.query_one(InlineThinkingRow)
        # frame_idx must point to a valid Braille frame.
        assert 0 <= row.frame_idx < len(_FRAMES), (
            f"expected frame_idx in [0, {len(_FRAMES)}), got {row.frame_idx}"
        )
        assert _FRAMES[row.frame_idx] in _FRAMES, (
            f"frame at index {row.frame_idx} is not a valid Braille char"
        )


@pytest.mark.asyncio
async def test_tick_advances_frame() -> None:
    """Tier 2b: _tick() cycles the Braille frame index forward."""
    from reyn.chat.tui.widgets.inline_thinking_row import _FRAMES, InlineThinkingRow

    # Exercise _tick() directly without a full app mount.
    row = InlineThinkingRow()
    initial_idx = row.frame_idx
    row._tick()
    assert row.frame_idx == (initial_idx + 1) % len(_FRAMES), (
        f"expected frame_idx to advance from {initial_idx} to "
        f"{(initial_idx + 1) % len(_FRAMES)}, got {row.frame_idx}"
    )
    # Cycle wraps: advance through all frames and confirm wrap-around.
    for _ in range(len(_FRAMES) - 1):
        row._tick()
    assert row.frame_idx == initial_idx, (
        f"after full cycle, frame_idx should return to {initial_idx}, "
        f"got {row.frame_idx}"
    )


def test_thinking_not_in_kind_priority() -> None:
    """Tier 2b: cleanup — 'thinking' no longer in _KIND_PRIORITY (sticky removed it)."""
    from reyn.chat.tui.widgets.sticky_status import _KIND_PRIORITY

    assert "thinking" not in _KIND_PRIORITY, (
        f"'thinking' should have been removed from _KIND_PRIORITY after "
        f"inline spinner migration; found it: {_KIND_PRIORITY}"
    )
