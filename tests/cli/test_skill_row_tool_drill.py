"""Tier 2: 2-level drill-down — SkillActivityRow surfaces its tool calls.

Follow-on to PR #546 (= phase-history drill-down) + PR #547
(= F3 keyboard expand). The phase drill-down shows "what
phases ran"; this PR adds the second axis — "what tools ran
under each phase" — by recording each tool_call_started event
that fires with a matching parent_run_id and rendering them
as a new line below the phase-history row.

Expanded view after this PR:

  ▶ code_review#abc1  · reviewing  3.2s
    ↳ phases: plan(0.5s) → research(1.4s) → reviewing*(now)
    ↳ tools (3): file:read(*.py), file:grep("foo"), bash:run

Public surfaces tested:
  - ``SkillActivityRow.record_tool_call(tool, args)`` appends
    to the row's tool-call history
  - Expanded render includes ``↳ tools (N):`` followed by the
    recorded tool names
  - Args snippet truncated at ~14 chars (= readable inline)
  - Beyond _TOOL_DRILL_MAX_RENDER (= 6), a "+N more" suffix
    appears so the row stays compact
  - Collapsed view does NOT include the tools line
  - Skills with no tool calls do NOT show a misleading
    "tools (0):" row
  - End-to-end: ``conv.start_tool_call_row`` with
    ``parent_run_id`` matching a mounted skill row propagates
    the tool name to that skill's drill-down
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _rendered(row) -> str:
    return row.rendered_text()


@pytest.mark.asyncio
async def test_record_tool_call_appears_in_expanded_render() -> None:
    """Tier 2: ``record_tool_call`` adds the tool name to the expanded view."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="aaaa1111", skill_name="t_skill")
        row.set_phase("execute")
        row.record_tool_call("file:read", "")
        row.record_tool_call("bash:run", "npm test")
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        assert "↳ tools (2):" in text
        assert "file:read" in text
        assert "bash:run" in text


@pytest.mark.asyncio
async def test_collapsed_view_omits_tools_line() -> None:
    """Tier 2: collapsed render does NOT include the tools drill-down."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="bbbb2222", skill_name="collapsed")
        row.set_phase("execute")
        row.record_tool_call("file:read", "")
        await pilot.pause()
        assert row.is_expanded is False
        text = _rendered(row)
        assert "↳ tools" not in text


@pytest.mark.asyncio
async def test_expanded_with_no_tools_omits_tools_line() -> None:
    """Tier 2: expanded view of a skill with no tool calls hides the row.

    Without the gate, the expanded render would show a
    misleading "tools (0):" row for skills that didn't fire
    any tools.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="cccc3333", skill_name="no_tools")
        row.set_phase("execute")
        # No record_tool_call calls.
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        # Phase history line still present.
        assert "↳ phases:" in text
        # Tools line suppressed.
        assert "↳ tools" not in text


@pytest.mark.asyncio
async def test_tool_args_snippet_truncated_for_long_args() -> None:
    """Tier 2: long args repr gets shortened with ``…`` so the line stays compact."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="dddd4444", skill_name="truncate")
        row.set_phase("execute")
        long_args = "command=very-long-command-with-lots-of-args"
        row.record_tool_call("bash:run", long_args)
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        # The full long args string should NOT appear verbatim in
        # the rendered line (= truncated).
        assert long_args not in text
        assert "bash:run" in text
        # Truncation marker visible.
        assert "…" in text


@pytest.mark.asyncio
async def test_tool_drill_renders_plus_n_more_beyond_cap() -> None:
    """Tier 2: tool call count beyond the cap collapses to "+N more"."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.skill_activity import _TOOL_DRILL_MAX_RENDER

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="eeee5555", skill_name="many_tools")
        row.set_phase("execute")
        # Add cap + 3 tools = 3 over the cap.
        over = _TOOL_DRILL_MAX_RENDER + 3
        for i in range(over):
            row.record_tool_call(f"tool_{i}", "")
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        # Total count appears in the prefix.
        assert f"↳ tools ({over}):" in text
        # "+3 more" suffix indicates 3 hidden beyond the cap.
        assert "+3 more" in text
        # The first tool is visible.
        assert "tool_0" in text
        # The last tool (= beyond cap) is NOT visible.
        assert "tool_8" not in text


@pytest.mark.asyncio
async def test_start_tool_call_row_propagates_to_skill_row() -> None:
    """Tier 2: end-to-end propagation from conv.start_tool_call_row.

    When the conv pane gets a ``tool_call_started`` for a tool
    whose ``parent_run_id`` matches a mounted skill row, the
    skill row's drill-down should record it. This is the path
    ``app_outbox._on_tool_call_started`` drives in production.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        skill_run_id = "ffff6666"
        row = conv.start_skill_row(run_id=skill_run_id, skill_name="prod")
        row.set_phase("execute")
        # Production path: app_outbox calls conv.start_tool_call_row
        # with parent_run_id set to the running skill.
        conv.start_tool_call_row(
            op_id="op-1",
            tool_name="file:read",
            args_repr="path=app.py",
            parent_run_id=skill_run_id,
        )
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        assert "↳ tools (1):" in text
        assert "file:read" in text


@pytest.mark.asyncio
async def test_orphan_tool_call_does_not_record_on_any_skill() -> None:
    """Tier 2: tool calls without a matching parent_run_id don't accidentally
    attach to an unrelated skill row.

    Defensive — if the forwarder ever emitted a ``tool_call_started``
    with an empty or mismatching ``parent_run_id``, the call should
    NOT contaminate a different skill row's drill-down.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="abcd7777", skill_name="ours")
        row.set_phase("execute")
        # Tool call with NO parent_run_id → must not register on any row.
        conv.start_tool_call_row(
            op_id="op-orphan",
            tool_name="orphan:tool",
            args_repr="",
            parent_run_id="",
        )
        # Tool call with a parent that doesn't match → also no register.
        conv.start_tool_call_row(
            op_id="op-mismatch",
            tool_name="mismatch:tool",
            args_repr="",
            parent_run_id="not-our-run-id-xxxxxx",
        )
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = _rendered(row)
        assert "↳ tools" not in text
        assert "orphan:tool" not in text
        assert "mismatch:tool" not in text
