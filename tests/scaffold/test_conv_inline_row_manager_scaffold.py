"""Tier 2: _InlineRowManager state-sharing invariants (scaffold, tui-pr1).

triggered_by: conv-inline-row-manager-tui-pr1
removed_by:   conv-inline-row-manager-tui-pr1

Guards that ConversationView's thin-delegate methods correctly reach the
_InlineRowManager instance and that shared mutable state is consistent across
the boundary (= no accidental copy / shadow).

Public surfaces tested:
  - start_skill_row / finish_skill_row round-trip via ConversationView delegates
  - start_tool_call_row / complete_tool_call_row round-trip
  - fail_tool_call_row sets latest_failed_tool_row() consistently
  - _row_mgr instance is the same object throughout the widget's lifetime
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_skill_row_round_trip_via_delegates() -> None:
    """Tier 2: start + finish skill row via ConversationView public API."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.skill_activity import SkillActivityRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        row = conv.start_skill_row("run-abc", "my_skill")
        assert isinstance(row, SkillActivityRow)
        await pilot.pause()
        assert row.is_mounted

        in_flight = conv.in_flight_skill_rows()
        assert row in in_flight

        conv.finish_skill_row("run-abc", success=True, reason="done")
        assert row not in conv.in_flight_skill_rows()


@pytest.mark.asyncio
async def test_tool_call_row_fail_sets_latest_failed() -> None:
    """Tier 2: fail_tool_call_row updates latest_failed_tool_row via delegate."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        assert conv.latest_failed_tool_row() is None

        row = conv.start_tool_call_row("op-xyz", "bash:run", args_repr="cmd=bad")
        assert row is not None

        conv.fail_tool_call_row("op-xyz", error="non-zero exit")

        failed = conv.latest_failed_tool_row()
        assert failed is row


@pytest.mark.asyncio
async def test_row_mgr_is_stable_reference() -> None:
    """Tier 2: _row_mgr is the same object before and after row operations."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets._inline_row_manager import _InlineRowManager

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        mgr = conv._row_mgr
        assert isinstance(mgr, _InlineRowManager)

        conv.start_skill_row("run-1", "skill_a")
        conv.start_tool_call_row("op-1", "file:read")
        conv.finish_skill_row("run-1", success=True)
        conv.complete_tool_call_row("op-1")

        assert conv._row_mgr is mgr
