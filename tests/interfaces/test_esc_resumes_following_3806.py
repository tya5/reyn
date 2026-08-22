"""Tier 2b: ``esc`` is the way back to the newest output; sending is not (#3806).

Two decisions live here and only one of them is code.

**``esc`` resumes following** — but only from an empty composer. It is the last
rung of a ladder whose upper rungs each own something to dismiss (the
completion popup, the sent queue's focus, the intervention panel, the drawer);
what reaches the bottom is an ``esc`` nobody else wanted, and "nothing to
dismiss" is answered by going back to the live output. With a draft in the box
it does nothing at all: someone who typed and then pressed ``esc`` is reaching
for "never mind" on the text, and moving the view would answer a question they
did not ask.

**Sending does NOT resume following**, and that is deliberately against the
convention. Slack, Discord and a shell all jump to the bottom on send, because
in those interfaces the only place that can show the message left is inside the
scrolling region. reyn's sent queue and NOW row sit OUTSIDE it, so the reason
for the convention is absent — and a convention imported without its reason is
just a surprise. Pinned because the next person to notice reyn behaving
differently from Slack will be right about the difference and wrong about the
cause, and a test is the only thing standing where that reasoning goes.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
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


async def _pump(pilot, until) -> None:
    """Pump until ``until()`` holds.

    Unbounded per the testing policy: a slower machine only makes this slower,
    never wrongly satisfied.
    """
    while not until():
        await pilot.pause()
        await asyncio.sleep(0.01)


async def _leave_the_tail(app, transport, pilot, *, lines: int = 40) -> FlowView:
    """Put enough behind the reader that scrolling up genuinely leaves the tail."""
    await transport.push_event(
        Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
    )
    for i in range(lines):
        await transport.push_display(OutboxMessage(kind="agent", text=f"line {i}", meta={}))
    flow = app.query_one(FlowView)
    await _pump(pilot, lambda: len(flow.entries) >= lines and flow.max_scroll_y > 0)
    flow.scroll_to(y=0, animate=False)
    await _pump(pilot, lambda: not flow.following)
    return flow


@pytest.mark.asyncio
async def test_esc_from_an_empty_composer_goes_back_to_the_newest_output() -> None:
    """Tier 2b: the bottom rung of the esc ladder."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _leave_the_tail(app, transport, pilot)

        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("escape")
        await _pump(pilot, lambda: flow.following)

        assert flow.following


@pytest.mark.asyncio
async def test_esc_with_a_draft_in_the_box_moves_nothing() -> None:
    """Tier 2b: a draft makes esc mean "never mind the text", not "scroll".

    "esc did nothing" has no positive predicate of its own to wait on, so it
    is proven via a causal successor instead of elapsed time: a real,
    positively-observable event is pushed through the SAME message pump right
    after the escape keypress, and this test waits (unbounded) for THAT to
    land. Textual's pump processes messages in order, so once the marker
    entry is rendered, escape's own handler (had it done anything) has
    already run — the negative checks below reflect settled state, not an
    early sample.

    Paired with the positive test above: that one establishes that this
    sequence DOES resume following when the box is empty, so a False here is
    the draft's doing and not a dead keypress.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _leave_the_tail(app, transport, pilot)
        entries_before = len(flow.entries)

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        composer.text = "a draft"
        await pilot.pause()
        await pilot.press("escape")
        await transport.push_display(
            OutboxMessage(kind="agent", text="after-escape marker", meta={})
        )
        await _pump(pilot, lambda: len(flow.entries) > entries_before)

        assert not flow.following, (
            "esc scrolled the conversation while there was a draft in the box"
        )
        assert composer.text == "a draft", (
            "esc cleared the draft — it is supposed to do nothing at all here"
        )


@pytest.mark.asyncio
async def test_sending_does_not_go_back_to_the_newest_output() -> None:
    """Tier 2b: the deliberate departure from the Slack/Discord convention.

    Sending while reading back leaves the view where the reader put it. What
    answers "did it send" is the sent queue and the NOW row, both outside the
    scrolling region — which is exactly the thing those other interfaces do not
    have, and the whole reason they jump.

    "sending did not resume following" has no positive predicate of its own,
    so it is proven via a causal successor (same pattern as the draft-esc
    test above): a real event is pushed through the SAME message pump right
    after "enter", and this test waits (unbounded) for THAT to land before
    checking the negative.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _leave_the_tail(app, transport, pilot)
        entries_before = len(flow.entries)

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        composer.text = "a message"
        await pilot.pause()
        await pilot.press("enter")
        await transport.push_display(
            OutboxMessage(kind="agent", text="after-send marker", meta={})
        )
        await _pump(pilot, lambda: len(flow.entries) > entries_before)

        assert not flow.following, (
            "sending returned to the tail — the convention was restored without "
            "the reason that justifies it elsewhere (see this module's docstring)"
        )
