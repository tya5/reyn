"""Tier 2: #1950 — a resize storm coalesces into ONE debounced log repaint.

A rapid terminal-resize (SIGWINCH) storm corrupts the RichLog's rendered TTY
output (dropped characters) via a Textual compositor issue; ``log.lines`` stays
intact. ConversationView.on_resize schedules a debounced full repaint that
re-renders from those intact lines AFTER the storm settles, healing the display.

The heal itself is a real-terminal compositor effect (NOT reproducible in a
headless pilot — the data model is correct, only the TTY writes drop chars), so
this pins the part that IS deterministically testable: the storm of resize events
coalesces into exactly ONE repaint, deferred until the storm settles (not one
repaint per event, and not a synchronous repaint on the first event). No mocks —
a real recording callable observes the widget's own repaint hook.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.events import Resize
from textual.geometry import Size

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_resize_storm_coalesces_to_one_deferred_repaint(monkeypatch) -> None:
    """Tier 2: #1950 — N rapid resize events → exactly one repaint, after settle."""
    import reyn.interfaces.tui.widgets.conversation as conv_mod
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    # Shrink the debounce so the coalesced repaint lands fast in the test.
    monkeypatch.setattr(conv_mod, "_RESIZE_REPAINT_DEBOUNCE_S", 0.05)

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Recording seam: wrap the widget's own repaint hook with a real callable
        # that counts + delegates (not a mock — observes the SUT's behaviour).
        repaints = 0
        real_repaint = conv._force_log_repaint

        def _recording_repaint() -> None:
            nonlocal repaints
            repaints += 1
            real_repaint()

        conv._force_log_repaint = _recording_repaint  # type: ignore[method-assign]

        # A resize storm — many rapid on_resize events (drag-resizing the window).
        sz = Size(80, 24)
        for _ in range(8):
            conv.on_resize(Resize(sz, sz))

        # Deferred: the first event does NOT repaint synchronously.
        assert repaints == 0, "repaint must be debounced, not fired on the first event"

        # After the storm settles (past the shrunk debounce), exactly ONE repaint —
        # the storm coalesced (not one repaint per event). The delegated real hook
        # also exercises refresh(repaint=True) against the live RichLog (no error).
        for _ in range(30):
            await pilot.pause(0.02)
            if repaints:
                break
        assert repaints == 1, (
            f"a resize storm must coalesce into one repaint, got {repaints}"
        )
