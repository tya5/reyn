"""Tier 2: ``ctrl+w`` / ``ctrl+shift+w`` / ``ctrl+shift+o`` are wired AND surfaced.

Right-panel deep-dive audit (MED severity Finding F1): the three panel-
cycling action methods (``action_panel_next_content``,
``action_panel_prev_content``) already existed on ``ReynTUIApp``, but
no ``Binding`` declarations in ``app.BINDINGS`` connected them to keys.
Because ``render_keys`` in the Keys tab iterates ``app.BINDINGS``, those
three keys were **permanently invisible** in the Keys tab — even though
the static ``_PANEL_KEYS`` set in ``keys_tab.py`` listed them.

The fix adds three ``Binding`` entries:
  • ``ctrl+w`` → ``panel_next_content`` ("Next tab")
  • ``ctrl+shift+w`` → ``panel_prev_content`` ("Prev tab")
  • ``ctrl+shift+o`` → ``panel_prev_content`` ("Prev tab (alt)")

The alias ``ctrl+shift+o`` exists because some terminals don't deliver
``ctrl+shift+w`` reliably — the action is the same, the key path is
the escape hatch.

These tests pin both that the bindings exist (= runtime keys fire) and
that the Keys tab renders them (= discoverability is preserved).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.binding import Binding

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys


# ── Binding existence ────────────────────────────────────────────────────────


def _bindings_by_key() -> dict[str, Binding]:
    """Index ``ReynTUIApp.BINDINGS`` by key for direct lookup."""
    out: dict[str, Binding] = {}
    for raw in ReynTUIApp.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        out[b.key] = b
    return out


def test_ctrl_w_is_bound_to_panel_next() -> None:
    """Tier 2: ``ctrl+w`` exists and routes to ``panel_next_content``."""
    b = _bindings_by_key().get("ctrl+w")
    assert b is not None, "ctrl+w must have a Binding entry"
    assert b.action == "panel_next_content", (
        f"ctrl+w must call action_panel_next_content; got {b.action!r}"
    )
    assert b.description, "ctrl+w needs a description so the Keys tab renders it"


def test_ctrl_shift_w_is_bound_to_panel_prev() -> None:
    """Tier 2: ``ctrl+shift+w`` exists and routes to ``panel_prev_content``."""
    b = _bindings_by_key().get("ctrl+shift+w")
    assert b is not None, "ctrl+shift+w must have a Binding entry"
    assert b.action == "panel_prev_content"
    assert b.description


def test_ctrl_shift_o_is_bound_as_prev_tab_alias() -> None:
    """Tier 2: ``ctrl+shift+o`` is an alias for prev-tab.

    Some terminals don't deliver ``ctrl+shift+w`` reliably; the alias
    gives users an alternative key path with the same effect.
    """
    b = _bindings_by_key().get("ctrl+shift+o")
    assert b is not None, "ctrl+shift+o alias must have a Binding entry"
    assert b.action == "panel_prev_content"
    assert b.description


# ── Keys tab renders them ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keys_tab_renders_ctrl_w_family() -> None:
    """Tier 2: the Keys tab's rendered output includes all three keys.

    ``render_keys`` iterates ``app.BINDINGS`` and emits ``pretty_key  desc``
    rows. With the new Binding entries, the rendered markup must contain
    the pretty forms of all three keys.
    """
    app = ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        rendered = render_keys(app)

        # Pretty forms per _KEY_PRETTY: ⌃W / ⌃⇧W / ⌃⇧O
        assert "⌃W" in rendered, (
            f"Keys tab must render ctrl+w; got:\n{rendered}"
        )
        assert "⌃⇧W" in rendered, (
            f"Keys tab must render ctrl+shift+w; got:\n{rendered}"
        )
        assert "⌃⇧O" in rendered, (
            f"Keys tab must render ctrl+shift+o alias; got:\n{rendered}"
        )


@pytest.mark.asyncio
async def test_keys_tab_groups_panel_keys_under_panel_section() -> None:
    """Tier 2: ``ctrl+w`` / ``ctrl+shift+w`` / ``ctrl+shift+o`` group under PANEL.

    ``_PANEL_KEYS`` in ``keys_tab.py`` declares these as PANEL-scope; the
    group header should land above them in the rendered output. Pins the
    grouping so a future refactor that moves them into ``_CONVERSATION_KEYS``
    or ``OTHER`` would surface.
    """
    app = ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        rendered = render_keys(app)

        # PANEL group header must appear, and ⌃W must come AFTER it.
        panel_idx = rendered.find("[PANEL]")
        ctrl_w_idx = rendered.find("⌃W")
        assert panel_idx >= 0, "[PANEL] group header missing"
        assert ctrl_w_idx > panel_idx, (
            f"⌃W must render under the PANEL group; got panel_idx={panel_idx} "
            f"⌃W_idx={ctrl_w_idx}"
        )


# ── check_action gating preserves the panel-visibility contract ─────────────


@pytest.mark.asyncio
async def test_panel_next_content_gated_on_panel_visible() -> None:
    """Tier 2: ``check_action`` returns False for the panel-cycle actions
    while the panel is hidden, True when visible.

    The existing gate (added in app.py:1198) covers ``panel_next_content``
    and ``panel_prev_content`` — both new bindings inherit it via the
    action name. Pin both states so the new aliases don't accidentally
    bypass the gate.
    """
    app = ReynTUIApp(
        registry=None,
        agent_name="test",
        model="test",
        budget_tracker=None,
    )
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        # Panel starts hidden — both should be gated off
        assert app.check_action("panel_next_content", None) is False
        assert app.check_action("panel_prev_content", None) is False

        # Open the panel
        app._panel_visible = True
        assert app.check_action("panel_next_content", None) is True
        assert app.check_action("panel_prev_content", None) is True
