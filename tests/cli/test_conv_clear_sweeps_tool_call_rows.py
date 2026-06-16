"""Tier 2: ConversationView.clear() sweeps in-flight ToolCallRow widgets (G-F1).

Wave-10 Topic G finding F1 (P1): ``clear()`` (= Ctrl+L) swept
``_stream_rows`` and ``_skill_rows`` but NOT ``_tool_call_rows``.
Tool-call widgets are mounted as direct children of ConversationView
(not lines in the RichLog), so ``_log().clear()`` doesn't unmount
them. Two visible failures followed:

  - the widget stayed on screen as an orphan over the now-blank
    pane (= same visual artefact ErrorBox had before its sweep was
    added)
  - ``_tool_call_rows`` still carried the stale op_id key, so when
    the in-flight tool completed, ``complete_tool_call_row`` /
    ``fail_tool_call_row`` popped the dict entry and called
    ``row.remove()`` against an already-orphaned widget → silent
    Textual DOM exception

After the fix ``clear()`` removes every ToolCallRow via
``finish_aborted("cleared") + remove()`` and empties the dict.

Public surfaces tested:
  - in-flight ToolCallRow is removed from the DOM after clear()
  - the ``_tool_call_rows`` dict is empty after clear()
  - subsequent ``complete_tool_call_row`` for the now-orphaned
    op_id does NOT raise (= dict pop returns None, idempotent)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_clear_removes_in_flight_tool_call_widgets() -> None:
    """Tier 2: after clear() the DOM has no ToolCallRow children."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView
    from reyn.tui.widgets.tool_call_row import ToolCallRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row(
            op_id="op-clear-1",
            tool_name="file__read",
            args_repr="path=/tmp/x",
        )
        await pilot.pause()
        # Sanity: the row is mounted + tracked.
        assert app.query(ToolCallRow), (
            "test scaffolding broken — ToolCallRow should be mounted"
        )
        assert "op-clear-1" in conv.tool_call_row_ids

        conv.clear()
        await pilot.pause()

        assert not app.query(ToolCallRow), (
            "ToolCallRow widget should be removed from DOM after clear()"
        )
        assert not conv.tool_call_row_ids, (
            f"tool_call_row_ids should be empty after clear(), "
            f"got {conv.tool_call_row_ids!r}"
        )


@pytest.mark.asyncio
async def test_tool_completion_after_clear_does_not_raise() -> None:
    """Tier 2: stale op_id completion is a no-op (idempotent).

    Pre-fix the dict entry survived clear(), so a late
    ``complete_tool_call_row`` would call ``row.remove()`` against
    an orphaned widget → exception. The dict-pop-returns-None
    pattern means a late completion now silently returns.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row(
            op_id="op-late-completion",
            tool_name="file__read",
            args_repr="path=/tmp/x",
        )
        await pilot.pause()

        conv.clear()
        await pilot.pause()

        # Late completion for the cleared op must not raise.
        conv.complete_tool_call_row(
            op_id="op-late-completion",
            result_snippet="status=ok",
        )
        await pilot.pause()
        # Dict stays empty.
        assert "op-late-completion" not in conv.tool_call_row_ids


@pytest.mark.asyncio
async def test_clear_with_no_tool_call_rows_is_no_op_safe() -> None:
    """Tier 2: clear() on a session with zero ToolCallRows still completes.

    Regression guard for ``for row in list({}.values())`` empty-iter.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No tool calls created — just clear.
        conv.clear()
        await pilot.pause()
        assert conv.tool_call_row_ids == frozenset()
