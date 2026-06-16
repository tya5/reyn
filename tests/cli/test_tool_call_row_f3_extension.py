"""Tier 2: F3 expand toggles ToolCallRow drill-down alongside SkillActivityRow.

Follow-on to PR #547 (= F3 keyboard for SkillActivityRow) +
PR #570 (= mouse-click drill-down for ToolCallRow). This PR
extends the F3 action so a single keypress flips drill-down on
EVERY in-flight inline row — skill rows AND tool call rows —
giving the user one keyboard trigger for the full "what's
happening right now" view.

Pinned:
  - ``ConversationView.in_flight_tool_call_rows()`` returns
    only the unfinished tool call rows
  - F3 (action_skill_expand_toggle) toggles in-flight tool call
    rows alongside skill rows
  - Mixed-state convergence: first row's state drives the
    target state, applied uniformly across BOTH widget kinds
  - Finished rows excluded from F3 target set (in both widget
    kinds — symmetrical with the skill-row behaviour from #547)
  - No-rows status hint now reads "no active rows to expand"
    (= reflects the extended scope)
  - Binding description updated to "Toggle inline row drill-down"

Compatibility notes:
  - Action name remains ``skill_expand_toggle`` to avoid breaking
    the existing #547 binding registration; only the
    behaviour + description widen. Old test asserting the
    binding name still passes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_in_flight_tool_call_rows_excludes_finished() -> None:
    """Tier 2: only unfinished tool call rows are returned."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        live = conv.start_tool_call_row(
            op_id="op-live", tool_name="bash:run", args_repr="",
        )
        done = conv.start_tool_call_row(
            op_id="op-done", tool_name="file:read", args_repr="",
        )
        done.finish_success()
        await pilot.pause()
        rows = conv.in_flight_tool_call_rows()
        assert live in rows
        assert done not in rows


@pytest.mark.asyncio
async def test_f3_toggles_in_flight_tool_call_rows() -> None:
    """Tier 2: F3 expands an in-flight tool call row (no skill rows present)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            op_id="op-1", tool_name="bash:run", args_repr="cmd=ls",
        )
        await pilot.pause()
        assert row.is_expanded is False
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row.is_expanded is True
        # Press again → collapse.
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert row.is_expanded is False


@pytest.mark.asyncio
async def test_f3_toggles_mixed_skill_and_tool_call_set() -> None:
    """Tier 2: F3 flips BOTH widget kinds with one keypress, converged.

    Pre-state: skill row collapsed, tool call row collapsed.
    F3 → both expanded. F3 again → both collapsed.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        skill = conv.start_skill_row(run_id="aaaa1111", skill_name="s")
        skill.set_phase("plan")
        tool = conv.start_tool_call_row(
            op_id="op-mix", tool_name="x", args_repr="",
        )
        await pilot.pause()
        assert skill.is_expanded is False
        assert tool.is_expanded is False

        app.action_skill_expand_toggle()
        await pilot.pause()
        # Both should be expanded after one keypress.
        assert skill.is_expanded is True
        assert tool.is_expanded is True

        app.action_skill_expand_toggle()
        await pilot.pause()
        # Both collapsed again.
        assert skill.is_expanded is False
        assert tool.is_expanded is False


@pytest.mark.asyncio
async def test_f3_convergence_across_mixed_states() -> None:
    """Tier 2: mixed expand state across kinds converges to one target.

    Pre-state: skill expanded, tool call collapsed. First key
    looks at the skill (= first in the list), target = collapsed.
    Both rows land collapsed.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        skill = conv.start_skill_row(run_id="bbbb2222", skill_name="s")
        skill.set_phase("plan")
        skill.toggle_expand()  # pre-expanded
        tool = conv.start_tool_call_row(
            op_id="op-mix2", tool_name="x", args_repr="",
        )
        await pilot.pause()
        assert skill.is_expanded is True
        assert tool.is_expanded is False

        app.action_skill_expand_toggle()
        await pilot.pause()
        # Skill was expanded → target = collapsed; both land collapsed.
        assert skill.is_expanded is False
        assert tool.is_expanded is False


@pytest.mark.asyncio
async def test_f3_skips_finished_tool_call_rows() -> None:
    """Tier 2: F3 only touches in-flight tool call rows, leaves finished ones."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        live = conv.start_tool_call_row(
            op_id="op-live2", tool_name="x", args_repr="",
        )
        done = conv.start_tool_call_row(
            op_id="op-done2", tool_name="y", args_repr="",
        )
        done.finish_success()
        await pilot.pause()
        assert live.is_expanded is False
        assert done.is_expanded is False
        app.action_skill_expand_toggle()
        await pilot.pause()
        assert live.is_expanded is True
        assert done.is_expanded is False  # untouched


@pytest.mark.asyncio
async def test_no_rows_status_hint_uses_neutral_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: status hint reflects the extended F3 scope ("rows" not "skill row").

    The first-use tip is pre-seeded as seen so the standard "no active
    rows" hint is what gets shown (= this test exercises the non-tip
    code path; first-use tip path is covered in test_f3_first_use_tip.py).
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.prefs import save_tui_prefs
    from reyn.interfaces.tui.widgets import ConversationView

    # Mark tip already seen so the standard "no rows" hint appears.
    save_tui_prefs(tmp_path, {"tip_f3_seen": True})

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No rows of either kind.
        assert conv.in_flight_skill_rows() == []
        assert conv.in_flight_tool_call_rows() == []
        app.action_skill_expand_toggle()
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        body = snap["body"]
        # Wording shift: no longer specific to "skill row".
        assert "no active" in body
        assert "rows" in body


def test_f3_binding_description_widened() -> None:
    """Tier 2: binding description reflects the extended F3 scope."""
    from reyn.interfaces.tui.app import ReynTUIApp

    for b in ReynTUIApp.BINDINGS:
        key = getattr(b, "key", None) or (b[0] if isinstance(b, tuple) else None)
        if key == "f3":
            description = getattr(b, "description", "") or ""
            assert "skill row" in description.lower()
            return
    raise AssertionError("F3 binding not found in ReynTUIApp.BINDINGS")
