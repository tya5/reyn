"""Tier 2: ToolCallRow drill-down — click to surface full args / result.

Categorical UX gap on the conv-pane execution-detail axis. Before
this PR, ToolCallRow truncated args + result to fit on 2 single-line
visual rows. Long tool calls (= heavy ``content=`` payloads, big
shell command lines, multi-key result dicts) lost the tail to ``…``.
Users could see the call happened but couldn't read what was
actually called or returned without switching to the right-panel
Events tab.

This adds a mouse-click toggle (mirrors the SkillActivityRow
drill-down from PR #546): clicking the row drops the cell-budget
truncation on both lines so the full content surfaces, with
Static-driven word-wrap letting the row grow vertically.

Public surfaces tested:
  - ``ToolCallRow.toggle_expand()`` flips ``is_expanded``
  - ``ToolCallRow.is_expanded`` reflects current state
  - ``on_click(event)`` triggers expand (mouse path)
  - Collapsed render truncates long args with ``…``
  - Expanded render surfaces the full args + result
  - Tool name un-elided in expanded view (= long
    qualified names like ``mcp__server__verb`` are shown in full)
  - Finished rows still expandable (= state preserved across
    success / failure terminals)
  - ToolCallRow inherits RenderableCacheMixin (= rendered_text
    accessor available, same idiom as SkillActivityRow + SlashPicker)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_row_starts_collapsed() -> None:
    """Tier 2: a freshly mounted row is not expanded."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            op_id="op-1", tool_name="file:read", args_repr="path=a.py",
        )
        await pilot.pause()
        assert row is not None
        assert row.is_expanded is False


@pytest.mark.asyncio
async def test_toggle_expand_flips_state() -> None:
    """Tier 2: ``toggle_expand`` round-trips collapsed↔expanded."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            op_id="op-2", tool_name="file:read", args_repr="path=a.py",
        )
        await pilot.pause()
        row.toggle_expand()
        assert row.is_expanded is True
        row.toggle_expand()
        assert row.is_expanded is False


@pytest.mark.asyncio
async def test_collapsed_view_truncates_long_args() -> None:
    """Tier 2: long args repr truncates with ``…`` in collapsed view."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        long_args = "cmd=" + ("x" * 100)
        row = conv.start_tool_call_row(
            op_id="op-3", tool_name="bash:run", args_repr=long_args,
        )
        await pilot.pause()
        text = row.rendered_text()
        # Full args NOT in collapsed view.
        assert long_args not in text
        # Ellipsis present somewhere as the truncation marker.
        assert "…" in text


@pytest.mark.asyncio
async def test_expanded_view_surfaces_full_args() -> None:
    """Tier 2: expanded view contains the entire args repr, no ``…`` cut."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        long_args = "cmd=" + ("x" * 100)
        row = conv.start_tool_call_row(
            op_id="op-4", tool_name="bash:run", args_repr=long_args,
        )
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = row.rendered_text()
        # Full args present.
        assert long_args in text


@pytest.mark.asyncio
async def test_expanded_view_un_elides_long_tool_name() -> None:
    """Tier 2: qualified tool names that get middle-elided collapsed are
    rendered in full when expanded."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    # Narrow terminal so the collapsed view triggers middle-elide.
    async with app.run_test(headless=True, size=(50, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        long_tool = "mcp__server-with-long-name__a_specific_verb"
        row = conv.start_tool_call_row(
            op_id="op-5",
            tool_name=long_tool,
            args_repr="x=1",
        )
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = row.rendered_text()
        # Full qualified name surfaces in expanded.
        assert long_tool in text


@pytest.mark.asyncio
async def test_expanded_view_surfaces_full_result_snippet() -> None:
    """Tier 2: long result snippets show in full when expanded."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        long_result = "output=" + ("y" * 100)
        row = conv.start_tool_call_row(
            op_id="op-6", tool_name="bash:run", args_repr="cmd=ls",
        )
        row.set_result(long_result)
        await pilot.pause()
        # Collapsed: truncated.
        collapsed = row.rendered_text()
        assert long_result not in collapsed
        row.toggle_expand()
        await pilot.pause()
        expanded = row.rendered_text()
        # Expanded: full result visible.
        assert long_result in expanded


@pytest.mark.asyncio
async def test_click_event_toggles_expand() -> None:
    """Tier 2: Click event invokes the expand toggle."""
    from textual import events as textual_events

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            op_id="op-7", tool_name="bash:run", args_repr="cmd=ls",
        )
        await pilot.pause()
        click = textual_events.Click(
            chain=1, widget=row, x=0, y=0, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False,
            screen_x=0, screen_y=0, style=None,
        )
        row.on_click(click)
        await pilot.pause()
        assert row.is_expanded is True


@pytest.mark.asyncio
async def test_finished_row_remains_expandable() -> None:
    """Tier 2: ``finish_success`` doesn't lock out the expand toggle."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(80, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        long_result = "result=" + ("z" * 100)
        row = conv.start_tool_call_row(
            op_id="op-8", tool_name="bash:run", args_repr="cmd=ls",
        )
        await pilot.pause()
        row.finish_success(result_snippet=long_result)
        await pilot.pause()
        # Pre-expand: truncated.
        assert long_result not in row.rendered_text()
        # User can still click + expand a completed row.
        row.toggle_expand()
        await pilot.pause()
        assert long_result in row.rendered_text()


@pytest.mark.asyncio
async def test_tool_call_row_inherits_renderable_cache_mixin() -> None:
    """Tier 2: ToolCallRow uses the shared RenderableCacheMixin from #568.

    Pins the inheritance — completes the 3rd Static-cache migration
    that the mixin extraction targeted (= SkillActivityRow + SlashPicker
    already migrated; ToolCallRow newly added here).
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets._renderable_cache import RenderableCacheMixin

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            op_id="op-9", tool_name="x", args_repr="",
        )
        await pilot.pause()
        assert isinstance(row, RenderableCacheMixin)
        # rendered_text accessor available from the mixin.
        assert isinstance(row.rendered_text(), str)
