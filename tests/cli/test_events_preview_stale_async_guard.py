"""Tier 2: events-tab async LLM preview fallback is dropped after navigation.

`_show_event_in_preview` shows YAML immediately on a sync-registry miss, then
spawns `_show_event_llm_fallback`, which `await`s an LLM viewer-template call
(seconds) and then writes the result into the shared preview pane. Before the
fix that write was UNCONDITIONAL — so if the user pressed j/k to move the
events cursor (or switched tabs) during the LLM call, event A's late result
clobbered event B's preview (showing "event #0 …" content while the cursor sat
on #3).

The fix threads a monotonic ``_preview_token`` (bumped by every
``_update_preview`` = any tab switch / cursor move / refresh) through the async
fallback; the result is written only via ``_write_preview_if_current``, which
drops it when the token has advanced. These tests pin that guarded-write
contract and the navigation → token-bump → stale-drop path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _CapturePane:
    """Minimal preview-pane stub recording show_text/clear calls (UI surface,
    not a collaborator with an API contract — no mock needed)."""

    def __init__(self) -> None:
        self.writes: list = []

    def show_text(self, title: str, renderable: object) -> None:
        self.writes.append((title, renderable))

    def clear(self) -> None:
        self.writes.append(("__clear__", None))


@pytest.mark.asyncio
async def test_write_preview_if_current_drops_stale_token() -> None:
    """Tier 2: a guarded write lands at the current token, is dropped otherwise."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        cur = panel._preview_token

        # Current token → the write lands.
        pane_ok = _CapturePane()
        wrote_current = panel._write_preview_if_current(
            pane_ok, "event #0", "body", cur,
        )
        assert wrote_current is True
        assert ("event #0", "body") in pane_ok.writes

        # An older (stale) token → suppressed, pane untouched.
        pane_stale = _CapturePane()
        wrote_stale = panel._write_preview_if_current(
            pane_stale, "event #0", "body", cur - 1,
        )
        assert wrote_stale is False
        assert pane_stale.writes == []


@pytest.mark.asyncio
async def test_navigation_bumps_token_so_inflight_result_is_dropped() -> None:
    """Tier 2: moving the events cursor bumps the token, stranding an in-flight
    async fallback that captured the pre-navigation token."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)

        # Two non-tool-result events so _update_preview takes the plain-YAML
        # path (no real LLM task spawned during this test).
        panel._panel_type = "events"
        panel._preview_visible = True
        panel._events_visible = [
            {"type": "phase_started", "data": {"chain_id": "c"}},
            {"type": "phase_completed", "data": {"chain_id": "c"}},
        ]
        panel._events_cursor = 0

        # An async LLM fallback launched for event #0 would have captured this.
        token_for_event0 = panel._preview_token

        # User navigates to event #1 → _events_move → _update_preview bumps token.
        panel._events_move(1)
        await pilot.pause()

        # The late result for event #0 must now be dropped, not clobber #1.
        pane = _CapturePane()
        wrote = panel._write_preview_if_current(
            pane, "event #0", "stale-A-content", token_for_event0,
        )
        assert wrote is False, (
            "an async fallback that captured the pre-navigation token must be "
            "dropped after the cursor moved"
        )
        assert pane.writes == []
