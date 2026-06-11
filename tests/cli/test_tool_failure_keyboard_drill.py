"""Tier 2: F7 keyboard drill-down for the most-recent failed ToolCallRow.

Wave-13 T2-1 / audit finding A#5.

Tool failure rows showed the failure reason truncated to ~80 cells on
line 2; expanding them for the full trace required a mouse click — there
was no keyboard path. This adds F7 as the keyboard companion.

Public surfaces tested:
  - mount 1 success + 1 failure → action_drill_failed_tool() toggles
    only the failure row's expand state (not the success row).
  - mount 0 failed rows → action surfaces "no recent tool failure" hint.
  - F7 binding is registered in app BINDINGS.
  - toggle twice → expand state returns to its original value.
  - latest_failed_tool_row() returns None before any failure.
  - latest_failed_tool_row() returns the row after fail_tool_call_row().
  - Keys tab routes F7 to CONVERSATION group + pretty-prints as F7.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_drill_failed_tool_only_toggles_failure_row() -> None:
    """Tier 2: action toggles the failure row; success row stays collapsed."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Mount one success row and one failure row.
        success_row = conv.start_tool_call_row(
            op_id="ok-1", tool_name="file:read", args_repr="path=a.py",
        )
        failure_row = conv.start_tool_call_row(
            op_id="fail-1", tool_name="bash:run", args_repr="cmd=bad",
        )
        assert success_row is not None
        assert failure_row is not None

        # Finalise: success stays mounted; failure is finalised and tracked.
        success_row.finish_success(result_snippet="ok")
        # Use the public fail_tool_call_row path so _last_failed_tool_row is set.
        # (The row is popped from _tool_call_rows; we already hold the ref above.)
        failure_row.finish_failure(reason="exit code 1")
        conv._row_mgr._last_failed_tool_row = failure_row  # simulate tracking (tui-pr1: state moved to _row_mgr)

        await pilot.pause()

        # Pre-condition: both rows start collapsed.
        assert success_row.is_expanded is False
        assert failure_row.is_expanded is False

        app.action_drill_failed_tool()
        await pilot.pause()

        # Only the failure row expands.
        assert failure_row.is_expanded is True
        assert success_row.is_expanded is False


@pytest.mark.asyncio
async def test_drill_failed_tool_no_failure_shows_hint() -> None:
    """Tier 2: action with no failed row surfaces 'no recent tool failure'."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # No failures — latest_failed_tool_row() should return None.
        assert conv.latest_failed_tool_row() is None

        app.action_drill_failed_tool()
        await pilot.pause()

        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "no recent tool failure" in snap["body"]


def test_f7_binding_registered() -> None:
    """Tier 2: 'f7' is bound to 'drill_failed_tool' in app BINDINGS."""
    from reyn.chat.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    assert ("f7", "drill_failed_tool") in binds


@pytest.mark.asyncio
async def test_drill_failed_tool_toggle_twice_returns_original() -> None:
    """Tier 2: pressing F7 twice leaves the row in its original expand state."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        failure_row = conv.start_tool_call_row(
            op_id="fail-2", tool_name="bash:run", args_repr="cmd=bad",
        )
        assert failure_row is not None
        failure_row.finish_failure(reason="timeout")
        conv._row_mgr._last_failed_tool_row = failure_row  # tui-pr1: state moved to _row_mgr

        await pilot.pause()
        original_state = failure_row.is_expanded  # False

        # First press: expand.
        app.action_drill_failed_tool()
        await pilot.pause()
        assert failure_row.is_expanded is not original_state

        # Second press: collapse back.
        app.action_drill_failed_tool()
        await pilot.pause()
        assert failure_row.is_expanded is original_state


@pytest.mark.asyncio
async def test_latest_failed_tool_row_none_before_failure() -> None:
    """Tier 2: latest_failed_tool_row() is None when no failure occurred."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv.latest_failed_tool_row() is None


@pytest.mark.asyncio
async def test_latest_failed_tool_row_set_after_fail() -> None:
    """Tier 2: fail_tool_call_row() causes latest_failed_tool_row() to return the row."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        row = conv.start_tool_call_row(
            op_id="fail-3", tool_name="bash:run", args_repr="cmd=err",
        )
        assert row is not None
        assert conv.latest_failed_tool_row() is None

        # Use the public API to trigger failure tracking.
        conv.fail_tool_call_row("fail-3", error="permission denied")
        await pilot.pause()

        # latest_failed_tool_row() now returns the row.
        result = conv.latest_failed_tool_row()
        assert result is not None
        # The row is in failure terminal state.
        assert result.finished is True
        assert result.success is False


def test_keys_tab_routes_f7_to_conversation() -> None:
    """Tier 2: F7 lands in CONVERSATION group, pretty-prints as F7."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import (
        _key_group_for,
        _pretty_key,
    )

    assert _key_group_for("f7") == "CONVERSATION"
    assert _pretty_key("f7") == "F7"
