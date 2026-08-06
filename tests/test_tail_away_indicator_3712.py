"""#3712 — say how far behind the newest output the reader is, and how to return.

Scrolling back through a long reply stops the conversation following the tail.
Output keeps arriving and nothing says so: the pane looks like the end of the
conversation because, from where the reader is, it is.

The indicator rides in the live-turn row's spare space (#3693) rather than
claiming a row of its own — on the surface #3680 exists to protect, a permanent
row for an occasional state is expensive.

Two things this must not do, both of which the gates below pin:

- **name a key that does not work.** The conversation pane has `end` and `G`,
  but they fire only while IT holds focus — never from the composer, which is
  where a reader scrolling back actually is. So the hint names an app-level
  binding, and a gate presses that key rather than asserting the text.
- **count something the reader did not experience.** The unit is ENTRIES landed
  since they left, not rows: a row count would climb while a single streamed
  reply grew, reporting movement that never happened.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import (
    LATEST_HINT,
    ActivityRow,
    activity_text,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` (the shared test idiom)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


async def _settle(pilot, times: int = 5) -> None:
    for _ in range(times):
        await pilot.pause()


async def _fill_and_leave_the_tail(transport, pilot, app, *, lines: int = 40):
    """Put enough behind the reader that scrolling up genuinely leaves the tail."""
    await transport.push_event(
        Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
    )
    for i in range(lines):
        await transport.push_display(OutboxMessage(kind="agent", text=f"line {i}", meta={}))
    await _settle(pilot, 8)
    flow = app.query_one(FlowView)
    flow.scroll_to(y=0, animate=False)
    await _settle(pilot)
    assert flow.scroll_y < flow.max_scroll_y, (
        "the conversation did not actually leave the tail, so nothing below "
        "this is being exercised"
    )
    return flow


def test_the_indicator_takes_the_slot_ahead_of_the_cancel_hint() -> None:
    """Tier 1: when both want the right-hand slot, the LIVE count wins.

    Someone reading back cannot see the new output arriving; the cancel key
    works whether or not it is printed. So the more urgent one is shown.
    """
    with_behind = activity_text("RESPONDING", elapsed_s=5.0, width=78, behind=12)
    without = activity_text("RESPONDING", elapsed_s=5.0, width=78)

    assert "LIVE +12" in with_behind
    assert "Ctrl+C cancel" not in with_behind
    assert "Ctrl+C cancel" in without


def test_a_narrow_row_drops_the_hint_rather_than_cutting_it() -> None:
    """Tier 1: never a clipped key.

    `Ctrl+End latest` cut short still reads as a complete instruction and is a
    different one. The state survives; the suffix is printed whole or not at
    all.
    """
    narrow = activity_text("RESPONDING", elapsed_s=5.0, width=30, behind=12)

    assert "RESPONDING" in narrow
    assert LATEST_HINT not in narrow
    assert "LIVE" not in narrow


@pytest.mark.asyncio
async def test_nothing_is_shown_while_the_reader_is_on_the_newest_output() -> None:
    """Tier 2b: following the tail is the ordinary case and says nothing.

    An indicator that were always up would stop meaning "you are missing
    something".
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
        )
        for i in range(10):
            await transport.push_display(
                OutboxMessage(kind="agent", text=f"line {i}", meta={})
            )
        await _settle(pilot, 8)

        assert "LIVE" not in str(app.query_one(ActivityRow).render())


@pytest.mark.asyncio
async def test_output_arriving_while_away_is_counted() -> None:
    """Tier 2b: entries landing after the reader left are reported.

    The count starts when they leave, not from the beginning of the
    conversation — what matters is what they have not seen.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _fill_and_leave_the_tail(transport, pilot, app)

        for i in range(3):
            await transport.push_display(
                OutboxMessage(kind="agent", text=f"new {i}", meta={})
            )
        await _settle(pilot, 8)

        rendered = str(app.query_one(ActivityRow).render())
        assert "LIVE +3" in rendered, f"the arrivals were not reported: {rendered!r}"


@pytest.mark.asyncio
async def test_the_named_key_actually_returns_to_the_newest_output() -> None:
    """Tier 2b: the printed key is pressed, not assumed.

    The conversation pane's own `end`/`G` fire only while it holds focus, so a
    hint naming them would be wrong from the composer — which is where the
    reader is. Pressing the key the row prints is the only way this gate can
    tell a working affordance from a plausible-looking string.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _fill_and_leave_the_tail(transport, pilot, app)
        await transport.push_display(OutboxMessage(kind="agent", text="new", meta={}))
        await _settle(pilot, 8)
        assert "LIVE" in str(app.query_one(ActivityRow).render())

        await pilot.press("ctrl+end")
        await _settle(pilot, 6)

        assert flow.scroll_y >= flow.max_scroll_y, "the key did not return to the tail"
        assert "LIVE" not in str(app.query_one(ActivityRow).render()), (
            "the indicator survived the return it was pointing at"
        )
