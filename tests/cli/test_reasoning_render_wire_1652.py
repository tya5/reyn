"""Tier 2: #1652 reasoning signal → render WIRE (both paths, design A).

The producer emits a discrete ``kind="reasoning"`` outbox message immediately
before its reply. The handler stores it; the next reply render consumes it
path-appropriately:
  - STREAMING → an interactive ReasoningBlock mounted BEFORE the StreamingRow.
  - NON-STREAMING → static reasoning text written into the RichLog before the
    reply (a mounted widget would sit below the monolithic RichLog → wrong order).

These pin the handler→store→consume seam + the streaming DOM order (ReasoningBlock
precedes StreamingRow) on the public surface (mounted widgets / consume state),
not private internals.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage  # noqa: E402
from reyn.chat.tui.app import ReynTUIApp  # noqa: E402
from reyn.chat.tui.app_outbox import OutboxRouter  # noqa: E402
from reyn.chat.tui.widgets import ConversationView  # noqa: E402
from reyn.chat.tui.widgets.reasoning_block import ReasoningBlock  # noqa: E402
from reyn.chat.tui.widgets.streaming_row import StreamingRow  # noqa: E402


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="test-agent", model="test-model", budget_tracker=None,
    )


def _reasoning_msg(text: str) -> OutboxMessage:
    return OutboxMessage(
        kind="reasoning", text=text,
        meta={"reasoning": text, "source": "agent_reasoning"},
    )


def test_set_consume_pending_reasoning_roundtrip() -> None:
    """Tier 2: set_pending_reasoning stores; consume returns it once then None."""
    # No app needed — pure state on a fresh ConversationView.
    conv = ConversationView()
    assert conv.consume_pending_reasoning() is None      # nothing pending initially
    conv.set_pending_reasoning("deep thoughts")
    assert conv.consume_pending_reasoning() == "deep thoughts"
    assert conv.consume_pending_reasoning() is None      # consumed exactly once


@pytest.mark.asyncio
async def test_reasoning_signal_then_stream_mounts_block_before_streamrow() -> None:
    """Tier 2: streaming-path — a reasoning signal followed by begin_stream mounts
    an interactive ReasoningBlock positioned BEFORE the StreamingRow."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)

        router._on_reasoning(_reasoning_msg("thinking step by step"), conv, None)
        conv.begin_stream("msg-1", "reyn")             # the reply stream begins
        await pilot.pause()

        blocks = list(conv.query(ReasoningBlock))
        rows = list(conv.query(StreamingRow))
        assert blocks, "a ReasoningBlock must mount on the streaming path"
        assert "thinking step by step" in blocks[0].render_body().plain
        assert rows, "the StreamingRow must mount for the reply"
        # DOM order: ReasoningBlock precedes the StreamingRow (thoughts before reply).
        kids = list(conv.children)
        assert kids.index(blocks[0]) < kids.index(rows[0])
        # Pending was consumed (not left for a later turn).
        assert conv.consume_pending_reasoning() is None


@pytest.mark.asyncio
async def test_reasoning_signal_consumed_by_nonstreaming_agent_render() -> None:
    """Tier 2: non-streaming-path — a reasoning signal then an agent message
    consumes the pending reasoning (written as static text before the reply) —
    no ReasoningBlock widget mounts on this path (static-text treatment)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)

        router._on_reasoning(_reasoning_msg("reasoning before the answer"), conv, None)
        conv.render_message(OutboxMessage(kind="agent", text="the answer"))
        await pilot.pause()

        # Consumed by the agent render (not left pending, not a mounted widget).
        assert conv.consume_pending_reasoning() is None
        assert not list(conv.query(ReasoningBlock))     # static-text path, no widget


@pytest.mark.asyncio
async def test_empty_reasoning_signal_stores_nothing() -> None:
    """Tier 2: an empty reasoning signal leaves nothing pending (render-if-present
    — the handler's empty-guard, so a display-off / no-reasoning turn is inert)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        router._on_reasoning(OutboxMessage(kind="reasoning", text="", meta={}), conv, None)
        assert conv.consume_pending_reasoning() is None
