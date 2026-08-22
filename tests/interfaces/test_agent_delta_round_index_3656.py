"""Tier 2: a turn that calls a tool leaves TWO reply rows, not one.

A turn can produce more than one assistant message. Measured on a real one
(owner log, `2026-08-02T201131`, `chain_id=5038809f`): 140 deltas, three tool
calls, then 300 deltas — and the two texts land in history as two separate
assistant messages, 210 and 653 characters, with different content. The data
had two messages; only the display had one.

Coalescing keyed on `chain_id` alone, so the second round's deltas flowed into
the entry created before the tool row. What the model wrote AFTER reading a
tool result appeared ABOVE the call that produced it.

`round_index` comes from the producer, which runs inside the round
(`on_content_delta=self._emit_agent_delta`) — the boundary is a fact it holds,
not one a consumer reconstructs from the arrival order of unrelated frames. A
monotonic index rather than a flag: a dropped flag is undetectable, a dropped
index shows as a gap. Not `stop_reason` either — that says why a round ended,
and the boundary is its consequence.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (the idiom shared with ``tests/interfaces/test_3288_3c_tui_delta_coalesce.py``)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


_CHAIN = "chain-3656"


def _delta(text: str, round_index: int) -> Event:
    return Event(
        type="agent_delta",
        data={"text": text, "chain_id": _CHAIN, "round_index": round_index},
    )


def _reply_rows(app: TextualChatApp) -> "list[str]":
    """Every flow row belonging to this chain, in display order."""
    return [
        str(entry.item.text)
        for entry in app.query_one(FlowView).entries
        if (entry.item.meta or {}).get("chain_id") == _CHAIN
    ]


@pytest.mark.asyncio
async def test_text_before_and_after_a_tool_call_are_separate_rows() -> None:
    """Tier 2b: the owner's shape — deltas, tool calls, deltas — yields two rows.

    Shaped after the measured turn rather than a minimal two-delta case: three
    tool calls in ONE round is what the real log holds, and it is the reason a
    consumer cannot take "a tool frame arrived" as the boundary — it would have
    to further infer which of the three ended the round.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        for _ in range(3):
            await transport.push_event(_delta("before ", round_index=1))
        for i in range(3):
            await transport.push_display(
                OutboxMessage(kind="tool_call_started", text=f"tool {i}", meta={})
            )
        for _ in range(3):
            await transport.push_event(_delta("after ", round_index=2))
        await pilot.pause()

        rows = _reply_rows(app)

        # The property is SEPARATION, not a row count: no row may carry text
        # from both sides of the tool calls, and both texts must be somewhere.
        assert not any("before" in row and "after" in row for row in rows), (
            f"a round's text was merged with another round's: {rows!r}"
        )
        assert any("before" in row for row in rows)
        assert any("after" in row for row in rows)


@pytest.mark.asyncio
async def test_the_second_row_comes_after_the_tool_rows() -> None:
    """Tier 2b: order on screen matches order in time.

    Splitting into two rows is not enough on its own — the point is that the
    post-tool text sits BELOW the calls that produced it. A fix that made two
    rows but left the second one above the tool rows would satisfy the test
    above and still show the defect.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        await transport.push_event(_delta("before", round_index=1))
        await transport.push_display(
            OutboxMessage(kind="tool_call_started", text="tool", meta={})
        )
        await transport.push_event(_delta("after", round_index=2))
        await pilot.pause()

        texts = [str(e.item.text) for e in app.query_one(FlowView).entries]
        positions = {name: texts.index(name) for name in ("before", "tool", "after")}

        assert positions["before"] < positions["tool"] < positions["after"]


@pytest.mark.asyncio
async def test_a_turn_with_no_tool_call_still_coalesces_into_one_row() -> None:
    """Tier 2b: the single-round case is unchanged.

    This is the branch the new one is NOT — asserted because a turn without a
    tool call never leaves round 1, so the split branch is easy to leave
    unreached while everything stays green (verification-hazards §15). The two
    tests above are that branch's reachability witness; this one pins that
    reaching it did not cost the ordinary case.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        for chunk in ("one ", "two ", "three"):
            await transport.push_event(_delta(chunk, round_index=1))
        await pilot.pause()

        rows = _reply_rows(app)

        # One round's chunks must all land in the SAME row — the property the
        # split could plausibly break. Stated as "the chunks are not spread
        # across rows" rather than as a count, because what a row currently
        # displays is the repaint budget's business (#3570 defers text within
        # an interval), not this test's.
        assert rows, "a streamed reply must produce a row"
        assert all(row is rows[0] or row == rows[0] for row in rows), (
            f"a single-round turn was split across rows: {rows!r}"
        )


@pytest.mark.asyncio
async def test_a_delta_without_a_round_index_behaves_as_before() -> None:
    """Tier 2b: an older producer, or a replayed frame, still coalesces.

    The field is read with a default rather than required, so a frame that
    predates it degrades to the previous single-row behaviour instead of
    raising or opening a row per delta.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        for chunk in ("a", "b"):
            await transport.push_event(
                Event(type="agent_delta", data={"text": chunk, "chain_id": _CHAIN})
            )
        await pilot.pause()

        rows = _reply_rows(app)

        # The property is that the two deltas share a row, NOT what that row
        # currently displays: with no round_index they key alike, and #3570's
        # repaint budget decides whether the second chunk has been painted yet.
        # Asserting "ab" made this fail intermittently (2 of 6 runs) for a
        # reason unrelated to rounds — the same mistake the single-round test
        # above already corrected.
        assert rows, "a streamed reply must produce a row"
        assert all(row == rows[0] for row in rows), (
            f"deltas with no round_index were split across rows: {rows!r}"
        )


def test_the_round_index_survives_the_agui_boundary() -> None:
    """Tier 2: a remote client receives the field, not just the local TUI.

    Asserted because the AG-UI layer DECLARES this event's shape in
    ``profile.py`` ("carries the raw delta text + chain_id"), and a declaration
    is not a contract — if the encoder had been a field whitelist rather than a
    dict copy, the field would have been added, tested locally, and silently
    dropped on the wire. Measured rather than assumed.
    """
    import json

    from reyn.interfaces.transport.agui.protocol import (
        TextStreamTracker,
        encode_frame_wire_streaming,
    )

    events = encode_frame_wire_streaming(
        EventFrame(
            Event(
                type="agent_delta",
                data={"text": "hi", "chain_id": "c1", "round_index": 2},
            )
        ),
        TextStreamTracker(),
    )

    wire = json.dumps([getattr(e, "data", e) for e in events], default=str)

    assert '"round_index": 2' in wire, f"round_index did not reach the wire: {wire}"
