"""Tier 2: RewindMenuWidget tree mode — Phase-2 fork picker render/nav (2b).

The widget gains an always-tree mode (`from_tree_rows`) consuming
`build_branch_tree_rows` output: header rows are non-selectable decorators, the
cursor moves among checkpoint rows only. Pins selection-skips-headers + the
default = working-tree head + the tree render, via run_test real-DOM (mount-path
render is headless-testable; the key-driven flow is tui-coder's tmux scope).

Real RewindMenuWidget + real ConversationView mount — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView
from reyn.tui.widgets._branch_tree import build_branch_tree_rows
from reyn.tui.widgets.rewind_menu import _MAX_VISIBLE, RewindMenuWidget


def _rows_two_branches() -> list[dict]:
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 13, "parent_branch_id": None, "is_active": True},
        {"branch_id": 11, "fork_point_seq": 6, "head_seq": 10, "parent_branch_id": 0, "is_active": False},
    ]
    checkpoints = [
        {"seq": 3, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0},
        {"seq": 6, "ts": "", "kind": "phase", "anchor": "run tests", "branch_id": 0},
        {"seq": 12, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0},
        {"seq": 9, "ts": "", "kind": "turn", "anchor": "", "branch_id": 11},
    ]
    return build_branch_tree_rows(branches, checkpoints)


def test_default_selection_is_working_tree_head() -> None:
    """Tier 2: tree-mode default selection = first selectable = active newest
    (working-tree head), so Enter is immediately undo (Phase-1 parity)."""
    w = RewindMenuWidget.from_tree_rows(_rows_two_branches())
    pt = w.selected_point()
    assert pt is not None and pt["row"] == "checkpoint"
    assert pt["branch_id"] == 0   # the active branch
    assert pt["seq"] == 12        # active branch's newest checkpoint


def test_nav_skips_headers_visits_all_checkpoints() -> None:
    """Tier 2: ↑/↓ moves among checkpoint rows only (headers skipped); every
    checkpoint across branches is reachable, none lands on a header."""
    w = RewindMenuWidget.from_tree_rows(_rows_two_branches())
    seen = set()
    for _ in range(10):
        pt = w.selected_point()
        assert pt["row"] == "checkpoint"   # never a header
        seen.add(pt["seq"])
        w.move_selection(1)
    assert seen == {3, 6, 12, 9}           # all checkpoints reachable


def test_selected_point_never_a_header() -> None:
    """Tier 2: clamping at the ends keeps the selection on checkpoint rows."""
    w = RewindMenuWidget.from_tree_rows(_rows_two_branches())
    w.move_selection(-50)
    assert w.selected_point()["row"] == "checkpoint"
    w.move_selection(+50)
    assert w.selected_point()["row"] == "checkpoint"


@pytest.mark.asyncio
async def test_tree_render_shows_branches_and_caret() -> None:
    """Tier 2: mounted tree render shows both branch headers (active + inactive),
    the checkpoints, and the caret on the selection. run_test real DOM."""
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(120, 20)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        w = RewindMenuWidget.from_tree_rows(_rows_two_branches())
        await conv.mount(w)
        await pilot.pause()
        rendered = w.render().plain
        assert "active" in rendered and "inactive" in rendered   # both branch headers
        assert "fork @ #6" in rendered                            # inactive fork label
        assert "#12" in rendered and "#9" in rendered             # checkpoints both branches
        assert "▌" in rendered                                    # selection caret present


def test_empty_tree_safe() -> None:
    """Tier 2: empty tree rows → no selection, render does not crash."""
    w = RewindMenuWidget.from_tree_rows([])
    assert w.selected_point() is None
    w.move_selection(-1)
    assert "no checkpoints" in w.render().plain


def test_tree_render_shows_per_checkpoint_anchor() -> None:
    """Tier 2: the #1547 per-checkpoint anchor renders as a dim line UNDER its
    checkpoint row in tree mode (#1576 regression fix) — not only at fork-point
    branch headers. Single branch (no fork header), so the anchor can only
    surface via the per-row render; FAILS before the fix."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": 5, "ts": "", "kind": "turn", "anchor": "fix the auth bug", "branch_id": 0}]
    w = RewindMenuWidget.from_tree_rows(build_branch_tree_rows(branches, cps))
    rendered = w.render().plain
    assert "fix the auth bug" in rendered          # per-checkpoint anchor, not a header
    assert "#5" in rendered                          # ...under its checkpoint row


def test_tree_render_omits_empty_anchor() -> None:
    """Tier 2: a checkpoint with no anchor renders no dim anchor line (additive —
    parity with the old flat render)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": 5, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}]
    w = RewindMenuWidget.from_tree_rows(build_branch_tree_rows(branches, cps))
    lines = [ln for ln in w.render().plain.splitlines() if ln.strip()]
    # header + #5 row + footer hint = 3 non-empty lines; no extra anchor line.
    assert sum(1 for ln in lines if "#5" in ln) == 1


def test_tree_widget_can_focus_is_false() -> None:
    """Tier 2: trap 2 — the widget never steals focus from the InputBar
    (migrated from the removed flat 1f suite; #1563)."""
    assert RewindMenuWidget.from_tree_rows(_rows_two_branches()).can_focus is False


def test_tree_render_shows_kind_and_reltime() -> None:
    """Tier 2: each checkpoint row renders its kind label + a relative-time
    column (migrated from the removed flat 1f suite; #1563)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 6, "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": 5, "ts": "2026-06-13T00:00:00", "kind": "phase", "anchor": "", "branch_id": 0}]
    w = RewindMenuWidget.from_tree_rows(
        build_branch_tree_rows(branches, cps), rel_time_fn=lambda ts: "2m ago",
    )
    rendered = w.render().plain
    assert "#5" in rendered and "phase" in rendered and "2m ago" in rendered


def _tall_single_branch(n: int) -> list[dict]:
    """Tree rows for one active branch with ``n`` checkpoints (newest-first)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": n,
                 "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": i, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}
           for i in range(n)]
    return build_branch_tree_rows(branches, cps)


def test_tree_window_caps_visible_rows() -> None:
    """Tier 2: #1577 — a long tree windows to <=_MAX_VISIBLE rows with a
    '↓ N later' overflow marker — not all 30 rows render."""
    w = RewindMenuWidget.from_tree_rows(_tall_single_branch(30))
    rendered = w.render().plain
    cp_rows = [ln for ln in rendered.splitlines()
               if "#" in ln and "later" not in ln and "earlier" not in ln]
    assert len(cp_rows) <= _MAX_VISIBLE          # windowed, not 30
    assert "later" in rendered                    # overflow marker (selection at top)


def test_tree_window_keeps_selection_visible_and_pins_header() -> None:
    """Tier 2: #1577 — navigating deep keeps the selected checkpoint visible and
    pins its branch header (context) with an '↑ earlier' marker."""
    w = RewindMenuWidget.from_tree_rows(_tall_single_branch(30))
    w.move_selection(25)                          # deep into the list
    sel = w.selected_point()
    rendered = w.render().plain
    assert f"#{sel['seq']}" in rendered           # selected checkpoint visible
    assert "▌" in rendered                         # caret rendered
    assert "main" in rendered                      # branch header pinned (context)
    assert "earlier" in rendered                   # overflow-above marker


def test_small_tree_no_window() -> None:
    """Tier 2: #1577 — a tree within _MAX_VISIBLE renders fully, no markers."""
    w = RewindMenuWidget.from_tree_rows(_tall_single_branch(3))
    rendered = w.render().plain
    assert "earlier" not in rendered and "later" not in rendered
    for i in range(3):
        assert f"#{i}" in rendered
