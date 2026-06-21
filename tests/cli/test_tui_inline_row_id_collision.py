"""Tier 2: tool-call + streaming row widget IDs survive an 8-char-prefix clash.

Sibling class of the skillrow DuplicateIds fix (#1971). Both rows are deduped by
their FULL correlation id (op_id = args_hash for tool calls; msg_id for streams),
but the widget id was built from ``id[:8]`` — so two distinct ids sharing an
8-char prefix collapsed to one widget id and the second mount raised Textual
``DuplicateIds`` (swallowed → the row vanished). The widget id must track the
full (sanitized) correlation id.

Unlike the skillrow case (run_id[:8] = the date = a DETERMINISTIC clash), these
prefixes are hashes/ids so a real-world clash is birthday-rare — but it is the
same latent class and the full-id fix makes it collision-proof. The tests force
the clash to pin the invariant.

Public surface only: the mounted row widgets in the DOM and their ``id``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_tool_call_rows_prefix_clash_both_mount() -> None:
    """Tier 2: two op_ids sharing an 8-char prefix mount two distinct tool rows."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.tool_call_row import ToolCallRow

    # Share the first 8 chars, differ after → id[:8] collides pre-fix.
    op1 = "aabbccdd0001"
    op2 = "aabbccdd0002"
    assert op1[:8] == op2[:8] and op1 != op2, "clash precondition broken"

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.start_tool_call_row(op1, "file__read")
        conv.start_tool_call_row(op2, "file__write")
        await pilot.pause()

        rows = list(conv.query(ToolCallRow))
        row_count = len(rows)
        assert row_count == 2, f"both tool calls should mount a row; got {row_count}"
        ids = [r.id for r in rows]
        assert ids[0] != ids[1], (
            f"tool-call widget ids must be distinct per op_id; got {ids!r}"
        )


@pytest.mark.asyncio
async def test_stream_rows_prefix_clash_both_mount() -> None:
    """Tier 2: two msg_ids sharing an 8-char prefix mount two distinct stream rows."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.streaming_row import StreamingRow

    msg1 = "streamid0001"
    msg2 = "streamid0002"
    assert msg1[:8] == msg2[:8] and msg1 != msg2, "clash precondition broken"

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.begin_stream(msg1)
        conv.begin_stream(msg2)
        await pilot.pause()

        rows = list(conv.query(StreamingRow))
        row_count = len(rows)
        assert row_count == 2, f"both streams should mount a row; got {row_count}"
        ids = [r.id for r in rows]
        assert ids[0] != ids[1], (
            f"stream widget ids must be distinct per msg_id; got {ids!r}"
        )
