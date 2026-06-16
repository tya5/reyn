"""Tier 2: stream chunks route by their own msg_id, not the latest global.

Multi-agent UX audit (HIGH severity Finding F2): the outbox dispatcher
routed chunks via ``app._current_stream_id`` — a single-slot global
overwritten on every new ``__stream_start__``. Under concurrent streams
(e.g. a ``/attach`` switch fired mid-stream and a fresh turn started for
the new agent), late chunks from the original stream appended to the
NEW row because the global had already been replaced. The user saw
agent A's tokens dribbling into agent B's reply, with no indication
either was wrong.

The fix: each chunk / end message carries its own ``msg.meta["msg_id"]``
matching its ``__stream_start__``; the handler routes by that, not by
``app._current_stream_id``. The dict-of-rows in ``ConversationView``
already supports per-stream lookup — this just stops the dispatcher
from collapsing two streams onto whichever was last.

These tests drive the handlers directly with synthetic OutboxMessages
to pin the routing contract.
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
from reyn.tui.widgets import ConversationView, ReynHeader


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="agent-a",
        model="test-model",
        budget_tracker=None,
    )


def _start_msg(msg_id: str) -> OutboxMessage:
    return OutboxMessage(kind="__stream_start__", text="", meta={"msg_id": msg_id})


def _chunk_msg(msg_id: str, text: str) -> OutboxMessage:
    return OutboxMessage(kind="__stream_chunk__", text=text, meta={"msg_id": msg_id})


def _end_msg(msg_id: str) -> OutboxMessage:
    return OutboxMessage(kind="__stream_end__", text="", meta={"msg_id": msg_id})


# ── single-stream baseline ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_stream_chunks_land_on_their_row() -> None:
    """Tier 2: chunks for a single stream accumulate on the row keyed by msg_id."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_stream_start(_start_msg("A1"), conv, header)
        router._on_stream_chunk(_chunk_msg("A1", "hello "), conv, header)
        router._on_stream_chunk(_chunk_msg("A1", "world"), conv, header)
        await pilot.pause()

        row = conv.stream_rows.get("A1")
        assert row is not None
        assert row.full_text() == "hello world"


# ── the F2 bug: concurrent streams ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_late_chunk_from_old_stream_routes_to_its_own_row() -> None:
    """Tier 2: the user-visible Multi F2 scenario.

    Sequence:
      1. agent-a starts streaming (msg_id = A1)
      2. agent-a sends a chunk "from a "
      3. /attach mid-stream — agent-b starts streaming (msg_id = B1)
      4. agent-b sends a chunk "from b"
      5. A LATE agent-a chunk " — late" arrives

    With the old single-slot global, the late chunk went to B1's row.
    With the fix, it goes to A1's row where it belongs.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_stream_start(_start_msg("A1"), conv, header)
        router._on_stream_chunk(_chunk_msg("A1", "from a "), conv, header)

        # /attach happens; new stream starts. Old code's _current_stream_id
        # now points at B1.
        router._on_stream_start(_start_msg("B1"), conv, header)
        router._on_stream_chunk(_chunk_msg("B1", "from b"), conv, header)

        # Late chunk for A1 arrives AFTER B1 started. With the routing
        # fix, it lands on A1's row, not B1's.
        router._on_stream_chunk(_chunk_msg("A1", " — late"), conv, header)
        await pilot.pause()

        a_row = conv.stream_rows.get("A1")
        b_row = conv.stream_rows.get("B1")
        assert a_row is not None and b_row is not None
        assert a_row.full_text() == "from a  — late", (
            f"A's late chunk leaked into B's row; A.full_text={a_row.full_text()!r}"
        )
        assert b_row.full_text() == "from b", (
            f"B's row should be untouched; got {b_row.full_text()!r}"
        )


@pytest.mark.asyncio
async def test_end_of_older_stream_does_not_clear_current_pointer() -> None:
    """Tier 2: ending an older stream leaves the global ``_current_stream_id`` alone.

    If A's end fires after B's start, clearing the global would corrupt
    any code that reads ``_current_stream_id`` as "the newest in-flight
    stream". The fix conditions the clear on the id matching.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_stream_start(_start_msg("A1"), conv, header)
        router._on_stream_start(_start_msg("B1"), conv, header)
        # Latest is B1
        assert app.current_stream_id == "B1"

        # A1 finishes after B1 started
        router._on_stream_end(_end_msg("A1"), conv, header)
        # The latest pointer must NOT have been wiped to None
        assert app.current_stream_id == "B1", (
            f"older stream's end cleared the global; got {app._current_stream_id!r}"
        )

        # When the latest does end, the global clears
        router._on_stream_end(_end_msg("B1"), conv, header)
        assert app.current_stream_id is None


@pytest.mark.asyncio
async def test_chunk_with_no_msg_id_is_silently_dropped() -> None:
    """Tier 2: a malformed chunk (no msg_id in meta) is a no-op, not a crash.

    Defence against a forwarder bug or a third-party producer that omits
    the meta key — the handler must not raise into the outbox loop.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        # Empty meta → chunk has no msg_id
        bad_chunk = OutboxMessage(kind="__stream_chunk__", text="orphan", meta={})
        # Must not raise
        router._on_stream_chunk(bad_chunk, conv, header)
        await pilot.pause()
        # No row created
        assert conv.stream_rows == {}


@pytest.mark.asyncio
async def test_end_seals_only_the_named_stream() -> None:
    """Tier 2: ``end`` for msg_id A removes A from the stream rows dict, leaves others."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_stream_start(_start_msg("A1"), conv, header)
        router._on_stream_start(_start_msg("B1"), conv, header)
        router._on_stream_chunk(_chunk_msg("A1", "a-body"), conv, header)
        router._on_stream_chunk(_chunk_msg("B1", "b-body"), conv, header)
        await pilot.pause()

        router._on_stream_end(_end_msg("A1"), conv, header)
        await pilot.pause()
        assert "A1" not in conv.stream_rows
        assert "B1" in conv.stream_rows, (
            "ending A1 must not touch B1's row"
        )
