"""Tier 2: a tool-call row in its min-display flush window is removed by clear().

Stale-deferred-removal class (feedback_tui_deferred_timer_stale_removal_class).
``complete_tool_call_row`` POPS the row from ``_tool_call_rows`` and, if the row
hasn't met its 0.3s minimum display time, schedules a deferred flush
(``set_timer``) that later writes the row's lines + removes it.

Bug: during that flush window the row is popped-from-dict but still mounted, so
``clear()`` (which sweeps the dict + query-sweeps InterventionWidget, but NOT
tool-call rows) misses it. A Ctrl+L within ~0.3s of a tool completing left a
ghost tool row mounted AND let the pending timer write stale lines into the
freshly-cleared log.

Public surface only: the mounted ``ToolCallRow`` widgets in the DOM.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_clear_removes_tool_row_in_flush_window() -> None:
    """Tier 2: clear() removes a tool-call row still pending its min-display flush.

    Completes a just-mounted tool row (elapsed << 0.3s → a deferred flush is
    scheduled and the row stays mounted), then clears immediately. No tool-call
    row may survive the clear.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.tool_call_row import ToolCallRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.start_tool_call_row("op-flush-1", "file__read")
        await pilot.pause()
        # Complete immediately → elapsed << 0.3s → deferred flush scheduled,
        # row popped from the dict but still mounted.
        conv.complete_tool_call_row("op-flush-1", result_snippet="done")
        await pilot.pause()
        # Sanity: the row is still mounted (pending flush), i.e. the race
        # window is open.
        assert list(conv.query(ToolCallRow)), (
            "precondition: row should still be mounted during the flush window"
        )

        conv.clear()
        await pilot.pause()

        survivors = list(conv.query(ToolCallRow))
        assert not survivors, (
            "a tool-call row in its min-display flush window survived clear() "
            f"(ghost row / stale-write race); still mounted: {survivors!r}"
        )
