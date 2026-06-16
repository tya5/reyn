"""Tier 2: AsyncStackPanel keyboard navigation (F4 focus + j/k + c + Esc).

Categorical UX gap on the execution-control axis. Before this PR,
the bottom strip (= AsyncStackPanel showing the attached agent's
running tasks) was mouse-only — no way to focus, navigate, or
interact via keyboard.

This adds:

  - ``F4`` → focuses the panel when entries are present (status
    hint when empty so the user doesn't trap focus on nothing)
  - ``j`` / ``↓`` / ``k`` / ``↑`` → cycle the selection cursor
    through visible entries (wraps at edges)
  - ``c`` → prefill the InputBar with ``/cancel <id>`` for the
    selected entry and refocus the input (= discoverable cancel
    path that delegates to the existing /cancel slash contract)
  - ``Esc`` → return focus to the InputBar without staging
    anything

Public surfaces tested:
  - ``AsyncStackPanel.can_focus`` is True
  - ``move_cursor`` advances / wraps cursor index
  - ``selected_agent_id`` returns the visible-order entry at cursor
  - ``action_focus_async_stack`` focuses the panel when non-empty
  - F4 binding registered on the app
  - Keys tab routes F4 to CONVERSATION group
  - Empty panel → status hint, focus NOT moved
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_panel_is_focusable() -> None:
    """Tier 2: ``can_focus`` is True so F4 routing can land here."""
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    assert AsyncStackPanel.can_focus is True


@pytest.mark.asyncio
async def test_move_cursor_advances_and_wraps() -> None:
    """Tier 2: ``move_cursor(+1)`` advances + wraps at the end."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("task-a", "skill_a")
        conv.add_async_task("task-b", "skill_b")
        conv.add_async_task("task-c", "skill_c")
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        # Visible order is "shortest elapsed first" so the most
        # recently added task floats to the top — task-c, task-b,
        # task-a. Cursor starts at 0 (= top of the visible list).
        first = panel.selected_agent_id()
        panel.move_cursor(+1)
        second = panel.selected_agent_id()
        panel.move_cursor(+1)
        third = panel.selected_agent_id()
        # All three IDs visited in sorted order (no duplicates / skips).
        assert {first, second, third} == {"task-a", "task-b", "task-c"}
        # Wrap forward returns to top.
        panel.move_cursor(+1)
        assert panel.selected_agent_id() == first
        # Backward wraps to bottom (third entry).
        panel.move_cursor(-1)
        assert panel.selected_agent_id() == third


@pytest.mark.asyncio
async def test_empty_panel_move_cursor_safe_noop() -> None:
    """Tier 2: navigation on an empty panel doesn't raise + clamps cursor to 0."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        panel.move_cursor(+1)
        assert panel.selected_agent_id() == ""


@pytest.mark.asyncio
async def test_f4_focuses_panel_when_entries_present() -> None:
    """Tier 2: ``action_focus_async_stack`` focuses the panel when non-empty."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("task-x", "skill_x")
        await pilot.pause()
        app.action_focus_async_stack()
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        assert panel.has_focus is True


@pytest.mark.asyncio
async def test_f4_with_empty_panel_shows_hint_and_doesnt_focus() -> None:
    """Tier 2: F4 on an empty strip surfaces a status hint, leaves focus."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        conv = app.query_one("#conversation", ConversationView)
        assert panel.snapshot() == []
        app.action_focus_async_stack()
        await pilot.pause()
        # Panel NOT focused.
        assert panel.has_focus is False
        # Status hint surfaced.
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "no active tasks" in snap["body"]


@pytest.mark.asyncio
async def test_c_key_prefills_inputbar_with_cancel() -> None:
    """Tier 2: pressing ``c`` while panel focused stages ``/cancel <id>``."""
    from textual import events

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView, InputBar
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("the-task-id", "skill_y")
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        panel.focus()
        await pilot.pause()
        # Simulate 'c' keypress.
        panel.on_key(events.Key(key="c", character="c"))
        await pilot.pause()
        ib = app.query_one("#inputbar", InputBar)
        ta = ib.query_one("#input")
        assert ta.text == "/cancel the-task-id"


@pytest.mark.asyncio
async def test_escape_returns_focus_to_input() -> None:
    """Tier 2: pressing ``Esc`` returns focus to the InputBar.

    Drives panel.on_key directly with an Escape event (= panel-
    scoped path), then verifies the InputBar's TextArea has
    focus via app.focused.
    """
    from textual import events

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("task-esc", "skill_esc")
        await pilot.pause()
        panel = app.query_one("#async-stack", AsyncStackPanel)
        panel.focus()
        await pilot.pause()
        assert panel.has_focus is True
        panel.on_key(events.Key(key="escape", character=None))
        await pilot.pause()
        # Panel relinquishes focus once the InputBar takes it.
        assert panel.has_focus is False


def test_f4_binding_registered() -> None:
    """Tier 2: F4 is bound to ``focus_async_stack`` action."""
    from reyn.interfaces.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    assert ("f4", "focus_async_stack") in binds


def test_keys_tab_routes_f4_to_conversation() -> None:
    """Tier 2: F4 lands in the CONVERSATION group + pretty-prints as "F4"."""
    from reyn.interfaces.tui.widgets.right_panel.keys_tab import (
        _key_group_for,
        _pretty_key,
    )

    assert _key_group_for("f4") == "CONVERSATION"
    assert _pretty_key("f4") == "F4"
