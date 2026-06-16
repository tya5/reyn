"""Tier 2: on_tabs_tab_activated re-anchors the panel scroll on the new tab's cursor.

``#panel-scroll`` is a single ``VerticalScroll`` shared by every tab body.
Before this fix ``on_tabs_tab_activated`` only called ``_invalidate()``,
so the scroll position from the previous tab carried over — when the
new tab's cursor sat below the inherited viewport, the cursor row went
invisible until the user pressed j/k.

The fix dispatches to the active tab's existing ``_scroll_*_into_view``
helper after the invalidate. This test pins that dispatch contract
without re-asserting the helper's own (already-tested) scroll math:
spy on each helper via direct attribute substitution and verify that
activating each cursor tab invokes its own helper exactly once, and
that the no-cursor tabs (``keys`` / ``cost``) invoke none of them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import Tab, Tabs

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import RightPanel


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_scroll_helpers(panel: RightPanel) -> dict[str, list[None]]:
    """Replace each ``_scroll_*_into_view`` helper with a recorder.

    Direct attribute substitution per ``testing.ja.md`` (= no
    ``unittest.mock``). Returns a dict mapping the tab id to a list
    that grows by one each time the helper is invoked.
    """
    calls: dict[str, list[None]] = {
        "events":  [],
        "agents":  [],
        "memory":  [],
        "docs":    [],
        "pending": [],
        "keys":    [],
    }

    def _make_recorder(key: str):
        def _fake() -> None:
            calls[key].append(None)
        return _fake

    panel._scroll_events_into_view  = _make_recorder("events")   # type: ignore[method-assign]
    panel._scroll_agents_into_view  = _make_recorder("agents")   # type: ignore[method-assign]
    panel._scroll_memory_into_view  = _make_recorder("memory")   # type: ignore[method-assign]
    panel._scroll_docs_into_view    = _make_recorder("docs")     # type: ignore[method-assign]
    panel._scroll_pending_into_view = _make_recorder("pending")  # type: ignore[method-assign]
    panel._scroll_keys_into_view    = _make_recorder("keys")     # type: ignore[method-assign]
    return calls


def _make_tab_event(panel: RightPanel, tab_id: str) -> Tabs.TabActivated:
    """Build a ``Tabs.TabActivated`` carrying a Tab whose id matches ``tab_id``."""
    tabs = panel.query_one("#panel-tabs", Tabs)
    return Tabs.TabActivated(tabs, Tab(tab_id, id=tab_id))


@pytest.mark.asyncio
async def test_cursor_tab_activation_invokes_matching_scroll_helper(
    tmp_path,
) -> None:
    """Tier 2: each cursor-bearing tab activation calls its own helper exactly once."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        calls = _instrument_scroll_helpers(panel)

        for tab_id in ("events", "agents", "memory", "docs", "pending", "keys"):
            panel.on_tabs_tab_activated(_make_tab_event(panel, tab_id))
            await pilot.pause()
            # Exactly one call on the matching helper, zero on the others.
            assert calls[tab_id] == [None], (
                f"tab {tab_id!r} activation: expected 1 call on "
                f"_scroll_{tab_id}_into_view, got {calls!r}"
            )
            for other in calls:
                if other == tab_id:
                    continue
                assert calls[other] == [], (
                    f"tab {tab_id!r} activation leaked to _scroll_{other}_into_view: "
                    f"{calls!r}"
                )
            # Reset for next iteration so each tab is asserted independently.
            for k in calls:
                calls[k].clear()


@pytest.mark.asyncio
async def test_non_cursor_tab_activation_invokes_no_scroll_helper(
    tmp_path,
) -> None:
    """Tier 2: ``cost`` tab has no cursor → no helper called.

    Defends against an accidentally over-broad dispatch (= "scroll
    something just in case") that could re-anchor the scroll incorrectly
    when the user lands on a non-navigable tab.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        calls = _instrument_scroll_helpers(panel)

        for tab_id in ("cost",):
            panel.on_tabs_tab_activated(_make_tab_event(panel, tab_id))
            await pilot.pause()
            assert all(v == [] for v in calls.values()), (
                f"tab {tab_id!r} (no cursor) should not invoke any "
                f"_scroll_*_into_view; got {calls!r}"
            )
