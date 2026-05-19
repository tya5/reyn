"""Tier 2: RightPanel re-clamps _panel_width when the terminal resizes.

Pre-fix bug: ``_panel_width`` (the absolute column count cached on
``h`` / ``l``) survived terminal shrinks unchanged. A user who had
expanded the panel to ~145 cols on a 220-col terminal and then ran
``tmux resize-window -x 80`` was left with a 145-col panel overflowing
the 80-col window, crushing the conv pane to ~0 cols of usable space.

The fix routes terminal resize events through ``on_resize`` which
re-clamps against ``_max_panel_width()`` (= 66 % of new terminal
width, floor 40 cols).

This test pins the contract via direct attribute substitution (per
testing.ja.md "no MagicMock"):

1. Resize that SHRINKS the terminal below the cached panel width →
   ``_panel_width`` is clamped down to the new ceiling.
2. Resize that GROWS the terminal → cached width is preserved (no
   spurious re-expansion).
3. Pre-resize panel still at CSS default (``_panel_width == 0``) →
   handler skips work (= Textual reflows the 33 % default for free).
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


def _stub_max_width(panel: RightPanel, value: int) -> None:
    """Pin ``_max_panel_width`` to ``value`` so the test doesn't depend on
    the actual Textual ``app.size.width`` (= sometimes 0 in headless mode
    and the formula floors at 40).
    """
    def _fake() -> int:
        return value

    panel._max_panel_width = _fake  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_terminal_shrink_clamps_cached_panel_width() -> None:
    """Tier 2: cached width above the new max is clamped to the new ceiling."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_width = 145  # type: ignore[attr-defined]

        # Simulate terminal shrink: max drops to 52 cols (= 66% of 80).
        _stub_max_width(panel, 52)
        panel.on_resize(event=None)  # the handler ignores the event arg

        assert panel._panel_width == 52, (
            f"on_resize must clamp _panel_width down to the new max; "
            f"got {panel._panel_width}"
        )


@pytest.mark.asyncio
async def test_terminal_grow_preserves_cached_panel_width() -> None:
    """Tier 2: cached width below the new max is preserved as-is."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._panel_width = 60  # type: ignore[attr-defined]

        # Simulate terminal grow: max rises to 132 cols (= 66% of 200).
        _stub_max_width(panel, 132)
        panel.on_resize(event=None)

        assert panel._panel_width == 60, (
            f"on_resize must NOT re-expand a smaller cached width; "
            f"got {panel._panel_width}"
        )


@pytest.mark.asyncio
async def test_panel_at_css_default_is_left_alone() -> None:
    """Tier 2: ``_panel_width == 0`` (= CSS-default 33%) skips the re-clamp.

    Defends against an over-broad handler that would set the panel to
    the new max even when the user hadn't manually resized — that would
    silently move them off the CSS default, breaking the "cold-start
    width" contract.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # _panel_width default is 0 (= CSS-managed)
        assert panel._panel_width == 0

        _stub_max_width(panel, 52)
        panel.on_resize(event=None)

        # Still 0 — Textual handles the CSS-default reflow on its own.
        assert panel._panel_width == 0, (
            f"on_resize must not touch the CSS-default panel; "
            f"got {panel._panel_width}"
        )
