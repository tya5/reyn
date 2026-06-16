"""Tier 2: agents tab preview refreshes on live exec_state tick (H-F3).

Wave-10 Topic H finding F3 (P2): the main agents-tab tree updated
correctly on every ``update_exec_state`` call (= the 2s live tick
calls ``_invalidate`` → ``_panel_markup`` → ``render_agents`` →
rebuild of ``_agents_items``). But the preview pane snapshot was
captured once by ``_show_*_in_preview`` at navigation / Space
time and never refreshed. A user watching a long skill saw the
main tree tick ``elapsed: 104s`` while the preview still showed
``elapsed: 42s`` from when they opened it.

After the fix ``update_exec_state`` also calls ``_update_preview()``
when the agents tab is visible AND the preview pane is open. Scope
is gated on ``_panel_type == "agents"`` so other tabs don't pay
the cost.

Public surfaces tested:
  - update_exec_state with preview open + agents tab → _update_preview
    is called
  - update_exec_state with preview closed + agents tab → _update_preview
    is NOT called (= no wasted work)
  - update_exec_state with preview open + non-agents tab → _update_preview
    is NOT called (= scope guard intact)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_exec_state_tick_refreshes_open_preview_on_agents_tab() -> None:
    """Tier 2: with preview open on agents tab, tick → _update_preview fires."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._preview_visible = True

        calls: list[None] = []
        original = panel._update_preview

        def _spy() -> None:
            calls.append(None)
            original()

        panel._update_preview = _spy  # type: ignore[method-assign]

        panel.update_exec_state(
            {"run-abc": {"agent_name": "a", "phase": "p1", "elapsed_s": 5}},
        )
        await pilot.pause()
        assert len(calls) >= 1, (
            "live exec_state tick with preview open should refresh the preview"
        )


@pytest.mark.asyncio
async def test_exec_state_tick_with_preview_closed_does_not_refresh() -> None:
    """Tier 2: regression guard — preview closed → no wasted update."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._preview_visible = False

        calls: list[None] = []
        original = panel._update_preview

        def _spy() -> None:
            calls.append(None)
            original()

        panel._update_preview = _spy  # type: ignore[method-assign]

        panel.update_exec_state(
            {"run-abc": {"agent_name": "a", "phase": "p1"}},
        )
        await pilot.pause()
        assert calls == [], (
            "preview-closed live tick should not call _update_preview"
        )


@pytest.mark.asyncio
async def test_exec_state_tick_on_non_agents_tab_skips_preview() -> None:
    """Tier 2: scope guard — non-agents tab + preview open → no refresh."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "events"  # not agents
        panel._preview_visible = True

        calls: list[None] = []
        original = panel._update_preview

        def _spy() -> None:
            calls.append(None)
            original()

        panel._update_preview = _spy  # type: ignore[method-assign]

        panel.update_exec_state(
            {"run-abc": {"agent_name": "a", "phase": "p1"}},
        )
        await pilot.pause()
        assert calls == [], (
            "events tab + exec_state tick should not call _update_preview"
        )
