"""Tier 2: Ctrl+1 .. Ctrl+7 jump directly to a right-panel tab.

Categorical UX gap on the right-panel navigation axis. Before
this PR, the only keyboard path to a specific tab was Ctrl+W /
Ctrl+Shift+W cycling — to reach the Cost tab from Events the
user had to press Ctrl+W four times. This adds direct-jump
keybindings using the browser / IDE tab convention:

  Ctrl+1 → Keys
  Ctrl+2 → Events
  Ctrl+3 → Agents
  Ctrl+4 → Memory
  Ctrl+5 → Cost
  Ctrl+6 → Docs
  Ctrl+7 → Pending

Key #N corresponds to ``PANEL_TYPES[N-1]`` so the visual tab
order is preserved.

Pinned:
  - Each Ctrl+N binding is registered with the matching
    ``panel_jump_<name>`` action
  - Each action sets the panel's active tab to its target
  - Quick-jump opens the panel if it's currently hidden (= one
    keypress works from a closed-panel state, not two)
  - When the panel is already open, jump just switches the tab
    without re-toggling visibility
  - Keys tab routes the Ctrl+N keys to the PANEL group with
    pretty-printed ``⌃N`` labels
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


_EXPECTED_BINDINGS = {
    "ctrl+1": ("panel_jump_keys", "keys"),
    "ctrl+2": ("panel_jump_events", "events"),
    "ctrl+3": ("panel_jump_agents", "agents"),
    "ctrl+4": ("panel_jump_memory", "memory"),
    "ctrl+5": ("panel_jump_cost", "cost"),
    "ctrl+6": ("panel_jump_docs", "docs"),
    "ctrl+7": ("panel_jump_pending", "pending"),
}


def test_quick_jump_bindings_registered() -> None:
    """Tier 2: each Ctrl+N maps to its ``panel_jump_<name>`` action."""
    from reyn.chat.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    for key, (action, _panel_type) in _EXPECTED_BINDINGS.items():
        assert (key, action) in binds, (
            f"{key} → {action} not registered; "
            f"got {[b for b in binds if b[0] == key]}"
        )


@pytest.mark.asyncio
async def test_panel_jump_switches_to_target_tab() -> None:
    """Tier 2: each action sets the panel's active tab to its target."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Walk each action; verify the active tab matches.
        action_map = {
            "keys":    app.action_panel_jump_keys,
            "events":  app.action_panel_jump_events,
            "agents":  app.action_panel_jump_agents,
            "memory":  app.action_panel_jump_memory,
            "cost":    app.action_panel_jump_cost,
            "docs":    app.action_panel_jump_docs,
            "pending": app.action_panel_jump_pending,
        }
        for panel_type, action in action_map.items():
            action()
            await pilot.pause()
            tabs = panel.query_one("#panel-tabs")
            assert tabs.active == panel_type, (
                f"Ctrl+jump → {panel_type} failed; active={tabs.active!r}"
            )


@pytest.mark.asyncio
async def test_quick_jump_opens_hidden_panel() -> None:
    """Tier 2: jump from a closed-panel state opens the panel + switches tab.

    The "one keypress from anywhere" UX would be broken if the
    user had to Ctrl+B first.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        # Panel starts hidden in the cold-default app config.
        assert app._panel_visible is False
        app.action_panel_jump_cost()
        await pilot.pause()
        assert app._panel_visible is True
        panel = app.query_one("#right_panel", RightPanel)
        tabs = panel.query_one("#panel-tabs")
        assert tabs.active == "cost"


@pytest.mark.asyncio
async def test_quick_jump_open_panel_does_not_re_toggle() -> None:
    """Tier 2: jump when panel already visible just switches tab.

    Pin that the toggle doesn't fire twice (= flip visibility off /
    on) when the panel is already open. The action_toggle_panel
    side-effect (focus rescue / DOM mutation) shouldn't run when
    we just want a tab switch.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        # Open the panel explicitly first.
        app.action_toggle_panel()
        await pilot.pause()
        assert app._panel_visible is True
        # Quick-jump to a different tab.
        app.action_panel_jump_agents()
        await pilot.pause()
        # Panel stays visible (= no spurious close-then-open flicker).
        assert app._panel_visible is True
        panel = app.query_one("#right_panel", RightPanel)
        tabs = panel.query_one("#panel-tabs")
        assert tabs.active == "agents"


def test_keys_tab_groups_quick_jump_under_panel() -> None:
    """Tier 2: Ctrl+1 .. Ctrl+7 land in the PANEL group + pretty-print as ⌃N."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import (
        _key_group_for,
        _pretty_key,
    )

    for n in range(1, 8):
        key = f"ctrl+{n}"
        assert _key_group_for(key) == "PANEL", (
            f"{key} should land in PANEL group; got {_key_group_for(key)}"
        )
        assert _pretty_key(key) == f"⌃{n}"


@pytest.mark.asyncio
async def test_keys_tab_render_includes_quick_jump_descriptions() -> None:
    """Tier 2: rendered Keys tab markup surfaces the ⌃N quick-jump descriptions."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _, _ = render_keys(app)
        for n in range(1, 8):
            assert f"⌃{n}" in markup, f"⌃{n} not in Keys tab markup"
        # Spot-check a couple of description strings.
        assert "Keys tab" in markup
        assert "Pending tab" in markup
