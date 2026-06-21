"""Tier 2: a tool-call row in its min-display flush window is removed by clear().

Stale-deferred-removal class (feedback_tui_deferred_timer_stale_removal_class).
``complete_tool_call_row`` POPS the row from ``_tool_call_rows`` and, if the row
hasn't met its minimum display time, schedules a deferred flush (``set_timer``)
that later writes the row's lines + removes it.

Bug: during that flush window the row is popped-from-dict but still mounted, so
``clear()`` (which sweeps the dict + query-sweeps InterventionWidget, but NOT
tool-call rows) misses it. A Ctrl+L within the min-display window of a tool
completing left a ghost tool row mounted AND let the pending timer write stale
lines into the freshly-cleared log.

Public surface only: the mounted ``ToolCallRow`` widgets in the DOM.

#2003 determinism: the flush window is driven by ``_TOOL_CALL_MIN_DISPLAY_S``
(default 0.3s) compared against the row's WALL-CLOCK mounted age. With the real
0.3s window the "row still mounted during the flush window" precondition was a
wall-clock race — under xdist parallel load a slowed worker let the window close
(timer fired / elapsed already exceeded 0.3s) before the assertion, intermittently
failing the precondition. We inject a large min-display window so the flush window
is OPEN for the whole (ms-scale) test regardless of worker speed: the precondition
holds deterministically and the clear()-removes-pending-row assertion (the #1980
contract) is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Large enough that the deferred-flush timer cannot fire during a ms-scale test
# even on a heavily-slowed xdist worker (and that mount→complete elapsed stays
# well under it → a deferred flush is always scheduled, never an immediate one).
_DETERMINISTIC_FLUSH_WINDOW_S = 3600.0


@pytest.mark.asyncio
async def test_clear_removes_tool_row_in_flush_window(monkeypatch) -> None:
    """Tier 2: clear() removes a tool-call row still pending its min-display flush.

    Completes a just-mounted tool row with the min-display window injected large
    (elapsed << window → a deferred flush is scheduled and the row stays mounted
    for the whole test), then clears immediately. No tool-call row may survive
    the clear. The injected window makes the flush-window precondition
    deterministic (no wall-clock vs worker-speed race); the assertion is the
    #1980 contract, unchanged.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView, _inline_row_manager
    from reyn.interfaces.tui.widgets.tool_call_row import ToolCallRow

    # Inject the flush window BEFORE any row completes. _flush_tool_call_row reads
    # this module global at call-time, so the deferred-flush path is taken with a
    # window that stays open for the whole test.
    monkeypatch.setattr(
        _inline_row_manager, "_TOOL_CALL_MIN_DISPLAY_S",
        _DETERMINISTIC_FLUSH_WINDOW_S,
    )

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.start_tool_call_row("op-flush-1", "file__read")
        await pilot.pause()
        # Complete → elapsed << injected window → deferred flush scheduled, row
        # popped from the dict but still mounted (the flush window is open).
        conv.complete_tool_call_row("op-flush-1", result_snippet="done")
        await pilot.pause()
        # Precondition (now deterministic): the row is still mounted, pending its
        # deferred flush — i.e. the race window #1980 closed is open here.
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
