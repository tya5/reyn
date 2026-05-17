"""Tier 2: ``RightPanel._refresh_live`` skips invalidation when panel is hidden.

Streaming / perf UX audit (MED severity Finding F4): the right panel's
2-second tick called ``_invalidate()`` whenever the active tab was in
``_LIVE_PANELS`` ({"events", "agents", "cost"}). But ``_invalidate`` for
the events tab walks every ``.reyn/events/*.jsonl`` to refresh mtime
caches — hundreds of ``stat()`` syscalls per tick on a long session.
The refresh fired unconditionally regardless of whether the panel was
even visible (``Ctrl+B`` collapses it).

The fix: gate ``_refresh_live`` on ``self.display`` so a hidden panel
pays zero refresh cost.

Tests pin:
  • When ``display`` is False, ``_refresh_live`` invokes neither
    ``_invalidate`` nor the panel-header / panel-content invalidations.
  • When ``display`` is True and the tab is live, ``_refresh_live``
    DOES invalidate.
  • Non-live tabs (e.g. ``keys``, ``docs``, ``memory``) never
    invalidate, even when visible — the original ``_LIVE_PANELS`` gate
    still applies.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import RightPanel


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_invalidate(panel: RightPanel) -> list[None]:
    """Replace ``panel._invalidate`` with a no-op counter. Returns the call list.

    Direct attribute substitution — no ``unittest.mock``. The list grows
    by one each time ``_invalidate`` is invoked.
    """
    calls: list[None] = []

    def _fake_invalidate() -> None:
        calls.append(None)

    panel._invalidate = _fake_invalidate  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_refresh_live_skips_when_panel_hidden() -> None:
    """Tier 2: hidden panel never triggers an invalidation.

    The panel starts with ``display: none`` (per its CSS); the toggle in
    ``app.py`` only sets ``display = True`` on Ctrl+B. Until then,
    ``_refresh_live`` ticks must be no-ops.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        assert panel.display is False, (
            "test setup: panel starts hidden via CSS display: none"
        )

        calls = _instrument_invalidate(panel)
        # Force a live tab so the panel_type gate isn't what skips us
        panel._panel_type = "events"

        panel._refresh_live()
        panel._refresh_live()
        panel._refresh_live()

        assert calls == [], (
            f"_refresh_live invoked _invalidate while panel was hidden: "
            f"{len(calls)} calls"
        )


@pytest.mark.asyncio
async def test_refresh_live_invalidates_when_visible_and_live_tab() -> None:
    """Tier 2: visible panel on a live tab DOES invalidate on tick.

    Pins the positive case so the gate fix doesn't accidentally suppress
    ALL refreshes — only hidden ones.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Open the panel (= Ctrl+B equivalent)
        panel.display = True
        await pilot.pause()
        panel._panel_type = "events"

        calls = _instrument_invalidate(panel)
        panel._refresh_live()
        panel._refresh_live()

        assert len(calls) == 2, (
            f"visible+live panel must invalidate; got {len(calls)} calls"
        )


@pytest.mark.asyncio
async def test_refresh_live_skips_non_live_tabs_even_when_visible() -> None:
    """Tier 2: the existing ``_LIVE_PANELS`` gate still applies when visible.

    Tabs like ``keys`` / ``docs`` / ``memory`` are content-static (or
    only change on user interaction); periodic re-invalidation would
    waste paint cycles for no signal. The visibility gate is additive
    — it tightens the filter, doesn't replace it.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel.display = True
        await pilot.pause()
        panel._panel_type = "keys"     # NOT in _LIVE_PANELS

        calls = _instrument_invalidate(panel)
        panel._refresh_live()
        panel._refresh_live()

        assert calls == [], (
            "non-live tab must still skip invalidation even when visible"
        )
