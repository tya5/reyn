"""Tier 2b: a tool-call failure sets the smart-Ctrl+B focal tab to events (C6).

C6 gap: ``OutboxRouter._on_tool_call_failed`` rendered the failed row but never
set ``_last_focal_tab``, so it stayed on whatever the last skill-trace event set
(= agents). Ctrl+B after a tool failure then opened the Agents tab instead of the
Events trace the user came to inspect. ``_on_error`` already set events; this
brings the tool-failure path in line.

Public surface: after a ``tool_call_failed``, opening the panel
(``action_toggle_panel`` = Ctrl+B) lands on the Events tab
(``RightPanel.panel_type == "events"``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.tui.app import ReynTUIApp
from reyn.tui.app_outbox import OutboxRouter
from reyn.tui.widgets import ConversationView
from reyn.tui.widgets.right_panel import RightPanel


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_tool_failure_sets_events_focal_tab_for_ctrl_b() -> None:
    """Tier 2b: Ctrl+B after a tool failure opens the Events tab (not Agents)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        # Precondition: a prior skill-trace event left the focal tab on
        # "agents" — the stale state the C6 bug would leave Ctrl+B pointing
        # at after a tool failure. (Setup only; the assert is on the public
        # panel_type after Ctrl+B.)
        app._last_focal_tab = "agents"
        msg = OutboxMessage(
            kind="tool_call_failed",
            text="",
            meta={
                "op_id": "op-fail-1",
                "error_kind": "Boom",
                "error_message": "kaboom",
            },
        )
        router._on_tool_call_failed(msg, conv, None)

        # Public surface: Ctrl+B opens the panel and lands on Events.
        app.action_toggle_panel()
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        assert panel.panel_type == "events", (
            f"Ctrl+B after a tool failure must open the Events tab; "
            f"got {panel.panel_type!r}"
        )
