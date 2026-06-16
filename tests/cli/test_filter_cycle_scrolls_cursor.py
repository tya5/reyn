"""Tier 2: cycle_event_filter re-anchors the events cursor into view.

Cycling the events filter (= ``f`` key) usually changes the visible
list and re-clamps ``_events_cursor`` to a different row. The previous
implementation only called ``_invalidate()`` and left the panel scroll
position alone, so the cursor sometimes ended up above / below the
viewport and the user lost the ``▶`` marker until they pressed j/k.

The fix calls ``_scroll_events_into_view`` after the invalidate. This
test pins that wiring at the public-API level: spy on the helper via
direct attribute substitution (per testing.ja.md "no MagicMock") and
verify the cycle method invokes it exactly once per call.

The same pattern as ``test_panel_tab_switch_scrolls_cursor.py`` —
permanent UX contracts (cursor-stays-visible across user actions)
land Tier 2 next to the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import RightPanel


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_events_scroll(panel: RightPanel) -> list[None]:
    """Replace ``_scroll_events_into_view`` with a call recorder.

    Direct attribute substitution per testing.ja.md (= no ``unittest.mock``).
    Returns a list that grows by one each time the helper is invoked.
    """
    calls: list[None] = []

    def _recorder() -> None:
        calls.append(None)

    panel._scroll_events_into_view = _recorder  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_cycle_event_filter_scrolls_cursor_into_view() -> None:
    """Tier 2: each ``cycle_event_filter`` call invokes the scroll helper once."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        calls = _instrument_events_scroll(panel)

        panel.cycle_event_filter()
        await pilot.pause()
        assert calls == [None], (
            f"cycle_event_filter must invoke _scroll_events_into_view exactly "
            f"once; got {len(calls)} calls"
        )

        # Subsequent cycles each fire the helper too (= the call is not a
        # one-shot side effect of the first invalidate).
        panel.cycle_event_filter()
        await pilot.pause()
        panel.cycle_event_filter()
        await pilot.pause()
        assert calls == [None, None, None], (
            f"each cycle must re-anchor the cursor; got {len(calls)} total"
        )
