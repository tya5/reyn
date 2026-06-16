"""Tier 2: cycle_event_tail re-anchors cursor in viewport.

Wave-11 finding A#4. Before this PR, ``cycle_event_filter``
called ``self._scroll_events_into_view()`` after re-rendering,
but its sibling ``cycle_event_tail`` did NOT — asymmetric
absence. Cycling 30→200 with cursor near the bottom of the old
window left the cursor invisible until the next j/k press
because the viewport stayed pinned at the old scroll position.

This PR adds the missing call (= single-line fix matching the
existing filter-cycle pattern).

Pinned:
  - ``cycle_event_tail`` invokes ``_scroll_events_into_view``
    after invalidating
  - The call order matches ``cycle_event_filter`` (invalidate
    first, then re-anchor)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_cycle_event_tail_calls_scroll_into_view() -> None:
    """Tier 2: ``cycle_event_tail`` invokes ``_scroll_events_into_view``."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Spy on the helper.
        call_count = {"n": 0}
        original = panel._scroll_events_into_view

        def _spy() -> None:
            call_count["n"] += 1
            original()

        panel._scroll_events_into_view = _spy  # type: ignore[method-assign]
        panel.cycle_event_tail()
        await pilot.pause()
        assert call_count["n"] >= 1


@pytest.mark.asyncio
async def test_cycle_event_filter_still_calls_scroll_into_view() -> None:
    """Tier 2: regression — the existing filter cycle still re-anchors."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        call_count = {"n": 0}
        original = panel._scroll_events_into_view

        def _spy() -> None:
            call_count["n"] += 1
            original()

        panel._scroll_events_into_view = _spy  # type: ignore[method-assign]
        panel.cycle_event_filter()
        await pilot.pause()
        assert call_count["n"] >= 1


@pytest.mark.asyncio
async def test_cycle_event_tail_advances_index() -> None:
    """Tier 2: regression — tail cycle still advances ``_event_tail_idx``."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel
    from reyn.interfaces.tui.widgets.right_panel import _TAIL_CYCLE

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        start = panel.event_tail_idx
        panel.cycle_event_tail()
        await pilot.pause()
        assert panel.event_tail_idx == (start + 1) % len(_TAIL_CYCLE)
