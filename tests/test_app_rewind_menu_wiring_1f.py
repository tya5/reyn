"""Tier 2: ReynTUIApp rewind-menu wiring — check_action gating + nav (1f).

The /rewind picker navigation (↑/↓/Enter) is app-driven via priority bindings
gated by ``check_action`` on ``_rewind_menu`` being mounted, so plain ↑/↓/Enter
fall through to the InputBar at all other times (trap 1 — the same gating
discipline as voice_stop_and_submit). Esc dismiss is multiplexed through
``check_action("voice_cancel")``.

These pin the gate truth-table + the nav/dismiss orchestration without a
running Textual event loop (ReynTUIApp constructs cheaply; check_action and the
nav actions don't need a mounted DOM). The gating is 1f-origin and
mode-agnostic; the widget is built via tree rows (the only mode since #1561 /
the flat path removed in #1563).

Real ReynTUIApp + real RewindMenuWidget — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets.branch_tree import build_branch_tree_rows
from reyn.tui.widgets.rewind_menu import RewindMenuWidget


def _menu(n: int) -> RewindMenuWidget:
    """A mounted-style picker over a single active branch with ``n`` checkpoints
    (tree mode; selection starts at index 0 = the newest, newest-first order)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": n,
                 "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": i, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}
           for i in range(n)]
    return RewindMenuWidget.from_tree_rows(build_branch_tree_rows(branches, cps))


def test_nav_actions_gated_off_when_menu_closed() -> None:
    """Tier 2: trap 1 — ↑/↓/Enter are inert until the picker is open, so they
    fall through to the InputBar (history / submit) the rest of the time."""
    app = ReynTUIApp()
    assert app.rewind_menu_open is False
    for action in ("rewind_prev", "rewind_next", "rewind_confirm"):
        assert app.check_action(action, ()) is False


def test_nav_actions_gated_on_when_menu_open() -> None:
    """Tier 2: trap 1 — ↑/↓/Enter become live once the picker is mounted."""
    app = ReynTUIApp()
    app._rewind_menu = _menu(3)
    for action in ("rewind_prev", "rewind_next", "rewind_confirm"):
        assert app.check_action(action, ()) is True


def test_esc_gate_includes_open_menu() -> None:
    """Tier 2: trap 1 — Esc (voice_cancel) fires while the menu is open so the
    priority Esc binding can dismiss it (the widget never sees Esc itself)."""
    app = ReynTUIApp()
    assert app.check_action("voice_cancel", ()) is False  # nothing to dismiss
    app._rewind_menu = _menu(2)
    assert app.check_action("voice_cancel", ()) is True


def test_nav_actions_move_selection() -> None:
    """Tier 2: action_rewind_prev/next drive the mounted widget's selection
    (tree: index 0 = newest; ↓ moves to older, ↑ clamps at the newest)."""
    app = ReynTUIApp()
    menu = _menu(4)                       # selection starts at 0 (newest)
    app._rewind_menu = menu
    app.action_rewind_next()
    assert menu.selected_index == 1
    app.action_rewind_prev()
    assert menu.selected_index == 0
    app.action_rewind_prev()             # clamp at the top, no wrap
    assert menu.selected_index == 0


def test_dismiss_clears_menu_state() -> None:
    """Tier 2: trap 4 — dismiss clears the menu state (decoupled unmount)."""
    app = ReynTUIApp()
    app._rewind_menu = _menu(2)
    assert app.rewind_menu_open is True
    app._dismiss_rewind_menu()
    assert app.rewind_menu_open is False
    # Idempotent — dismissing again is a no-op, not a crash.
    app._dismiss_rewind_menu()
    assert app.rewind_menu_open is False
