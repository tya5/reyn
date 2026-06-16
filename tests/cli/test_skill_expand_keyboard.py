"""Tier 2: F3 keyboard companion to SkillActivityRow drill-down.

Follow-on to PR #546 (= mouse-click drill-down). The mouse path
covers the primary UX but keyboard-driven users had no way to
trigger the expand without reaching for the mouse. This adds:

  - F3 toggles drill-down on every in-flight SkillActivityRow
  - When multiple rows are concurrent, F3 converges to a single
    target state (not per-row oscillation)
  - When no rows are in flight, F3 surfaces a status hint rather
    than silently no-op'ing
  - F3 is added to the Keys tab CONVERSATION group with a pretty
    "F3" label

Public surfaces tested:
  - ``ConversationView.in_flight_skill_rows()`` returns unfinished rows
  - ``action_skill_expand_toggle`` flips ``is_expanded`` on every
    in-flight row
  - Mixed-state set converges to a single state per keypress
  - Empty in-flight set → status sticky with usage hint
  - F3 binding registered with correct action name
  - Keys tab routes F3 to CONVERSATION group + pretty-prints as F3
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_in_flight_skill_rows_excludes_finished() -> None:
    """Tier 2: only unfinished rows are returned (= F3 target set)."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        live = conv.start_skill_row(run_id="aaaa1111", skill_name="live")
        done = conv.start_skill_row(run_id="bbbb2222", skill_name="done")
        done.finish(success=True, reason="ok")
        await pilot.pause()
        rows = conv.in_flight_skill_rows()
        ids = {r._run_id for r in rows}
        assert "aaaa1111" in ids
        assert "bbbb2222" not in ids
        assert live in rows
        assert done not in rows


@pytest.mark.asyncio
async def test_f3_action_expands_single_in_flight_row() -> None:
    """Tier 2: F3 toggles is_expanded on the lone in-flight row."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="cccc3333", skill_name="alone")
        row.set_phase("plan")
        await pilot.pause()
        assert row.is_expanded is False
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row.is_expanded is True
        # Pressing again collapses back.
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row.is_expanded is False


@pytest.mark.asyncio
async def test_f3_converges_mixed_state_to_single_target() -> None:
    """Tier 2: mixed-state set converges per keypress, not oscillates.

    With one row expanded and another collapsed, pressing F3 once
    should leave BOTH in the same state (= opposite of the first
    row's prior state). Without this convergence, naive per-row
    toggle would flip each individually and the next press would
    return to the original split — F3 would feel "stuck".
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row_a = conv.start_skill_row(run_id="dddd4444", skill_name="a")
        row_b = conv.start_skill_row(run_id="eeee5555", skill_name="b")
        row_a.set_phase("plan")
        row_b.set_phase("plan")
        # Pre-state: a expanded, b collapsed.
        row_a.toggle_expand()
        await pilot.pause()
        assert row_a.is_expanded is True
        assert row_b.is_expanded is False

        # F3 → converge. row_a was expanded → target = collapsed.
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row_a.is_expanded is False
        assert row_b.is_expanded is False

        # F3 again → both expanded.
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row_a.is_expanded is True
        assert row_b.is_expanded is True


@pytest.mark.asyncio
async def test_f3_with_no_in_flight_shows_status_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: F3 with no in-flight rows surfaces a status hint.

    The first-use tip is pre-seeded as seen so the standard "no active
    skill" hint is what gets shown (= this test exercises the non-tip
    code path; first-use tip path is covered in test_f3_first_use_tip.py).
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.prefs import save_tui_prefs
    from reyn.tui.widgets import ConversationView

    # Mark tip already seen so the standard "no rows" hint appears.
    save_tui_prefs(tmp_path, {"tip_f3_seen": True})

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No skill rows mounted.
        assert conv.in_flight_skill_rows() == []
        app.action_skill_expand_toggle()
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        # Body wording shifted from "skill row" to "rows" after F3
        # was extended to cover tool call rows too — substring match
        # on "no active" + "rows" survives the rename.
        assert "no active" in snap["body"]
        assert "rows" in snap["body"]


@pytest.mark.asyncio
async def test_f3_skips_finished_rows() -> None:
    """Tier 2: F3 only touches in-flight rows, leaves finished ones alone."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        live = conv.start_skill_row(run_id="ffff6666", skill_name="live")
        done = conv.start_skill_row(run_id="abcd7777", skill_name="done")
        live.set_phase("plan")
        done.set_phase("plan")
        done.finish(success=True, reason="ok")
        await pilot.pause()
        # Pre-state: both collapsed.
        assert live.is_expanded is False
        assert done.is_expanded is False
        app.action_skill_expand_toggle()
        await pilot.pause()
        # Live row toggled, done row untouched.
        assert live.is_expanded is True
        assert done.is_expanded is False


def test_f3_binding_registered() -> None:
    """Tier 2: ``f3`` is bound to ``skill_expand_toggle`` in app BINDINGS."""
    from reyn.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    assert ("f3", "skill_expand_toggle") in binds


def test_keys_tab_routes_f3_to_conversation() -> None:
    """Tier 2: F3 lands in the CONVERSATION group, not OTHER."""
    from reyn.tui.widgets.right_panel.keys_tab import (
        _key_group_for,
        _pretty_key,
    )

    assert _key_group_for("f3") == "CONVERSATION"
    # Pretty label is the proper capitalised "F3".
    assert _pretty_key("f3") == "F3"


@pytest.mark.asyncio
async def test_keys_tab_render_includes_f3_with_description() -> None:
    """Tier 2: rendered Keys tab markup surfaces F3 + its description."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _, _ = render_keys(app)
        assert "F3" in markup
        # Description uses "skill row drill-down" (main UI wording).
        assert "skill row drill-down" in markup.lower()
