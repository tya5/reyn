"""Tier 2: app_outbox tool-call handler → ToolCallRow render WIRE (#1642).

#1642 asked to surface tool-call content (args on start, result on completion)
in the conversation UI. The flow-trace finding was that the emitter already puts
``args``/``result`` in ``OutboxMessage.meta`` and the TUI ``ToolCallRow`` already
RENDERS args (line 1) + result (line 2) — so the TUI half needs no new render
code. What these pin is the one layer the existing tests don't exercise
end-to-end: the **handler composition** — ``OutboxRouter._on_tool_call_started``
reading ``meta["args"]`` → ``_format_tool_args`` → the mounted row's rendered
line, and the completed handler routing ``meta["result"]`` → line 2.

The widget-isolation tests (test_tool_call_row_*) construct a row with a
pre-formatted ``args_repr`` string; ``test_conv_pane_tool_call_lifecycle`` covers
the conv seam + the formatters directly. This file closes the gap between them:
the meta-keyed dispatch (``meta["args"]``/``meta["result"]`` contract) actually
reaching the rendered surface, so a rename of the meta key or a break in the
handler glue is caught (a regression guard for the #1642 conversation feature).

Asserts on the public render surface (``render_line1``/``render_line2`` → Rich
``Text.plain``), never private state.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp  # noqa: E402
from reyn.interfaces.tui.app_outbox import OutboxRouter  # noqa: E402
from reyn.interfaces.tui.widgets import ConversationView  # noqa: E402
from reyn.interfaces.tui.widgets.tool_call_row import ToolCallRow  # noqa: E402
from reyn.runtime.outbox import OutboxMessage  # noqa: E402


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_started_handler_renders_meta_args_in_row_line1() -> None:
    """Tier 2: a tool_call_started OutboxMessage whose meta carries ``args``
    renders the formatted args inside the ToolCallRow line-1 parens.

    Drives the real handler (``_on_tool_call_started``) — not a pre-formatted
    ``args_repr`` — so the ``meta["args"]`` → ``_format_tool_args`` → render
    composition (the #1642 conversation seam) is exercised end-to-end."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        msg = OutboxMessage(
            kind="tool_call_started",
            text="",
            meta={"op_id": "op-1", "tool": "file__read", "args": {"path": "a.py"}},
        )
        router._on_tool_call_started(msg, conv, None)
        await pilot.pause()

        rows = list(conv.query(ToolCallRow))
        assert rows, "a ToolCallRow must mount for the started event"
        line1 = rows[0].render_line1().plain
        assert "file__read" in line1                 # tool name
        assert "path=a.py" in line1                   # meta["args"] → formatted + rendered


@pytest.mark.asyncio
async def test_completed_handler_renders_meta_result_in_row_line2() -> None:
    """Tier 2: a tool_call_completed OutboxMessage whose meta carries ``result``
    renders the result preview on the ToolCallRow line 2.

    Holds the row reference from the started event, then drives the completed
    handler — pinning ``meta["result"]`` → ``_format_tool_result`` → line 2 (the
    completion half of the #1642 conversation seam)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        router._on_tool_call_started(
            OutboxMessage(
                kind="tool_call_started", text="",
                meta={"op_id": "op-2", "tool": "web__fetch", "args": {}},
            ),
            conv, None,
        )
        await pilot.pause()
        row = list(conv.query(ToolCallRow))[0]

        router._on_tool_call_completed(
            OutboxMessage(
                kind="tool_call_completed", text="",
                meta={"op_id": "op-2", "result": "200 OK 1.2KB"},
            ),
            conv, None,
        )
        # finish_success sets the snippet synchronously; assert before the
        # min-display unmount timer pumps the row out of the DOM.
        line2 = row.render_line2().plain
        assert "200 OK 1.2KB" in line2                # meta["result"] → formatted + rendered
