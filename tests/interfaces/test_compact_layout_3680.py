"""#3680 — a short terminal keeps a readable conversation.

Every region above the composer had its own height cap and none knew about the
others. Measured before this change, at 80x20 with three messages queued, a
turn running and the Help drawer open, the conversation was left **one row** —
and every region was inside its own limit. The caps were individually correct
and collectively wrong.

Two halves, gated separately because they fail differently:

- :func:`compact_caps` is a pure function of the height and what is open, so
  the policy itself can be asserted directly rather than inferred from a
  rendered screen. A policy bug shows up here as a number.
- the wiring is gated on a real app, because a correct policy computed from
  stale inputs is still wrong on screen — measured: the first version decided
  only when a region opened, which was a frame before the turn state landed,
  and gave the drawer one row too many.

What the policy may NOT do is drop content to make room. Everything it shrinks
still holds every item it had: the drawer, the picker and the completion popup
scroll (#3688, #3699), and the queue keeps every entry behind its count. That
line is gated too — #3688 is the record of what a region silently showing less
than it holds costs.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.compact import (
    COMPOSER_MIN,
    FLOW_MIN,
    compact_caps,
)
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
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


# ── the policy, without a terminal ───────────────────────────────────────────


def test_a_roomy_terminal_is_left_alone() -> None:
    """Tier 1: nothing is squeezed when nothing needs to be.

    A policy that shrank regions on a tall terminal would be paying the cost
    everywhere to fix the case that only happens somewhere.
    """
    caps = compact_caps(40, drawer_open=True, queue_items=3, turn_active=True)

    assert caps["drawer"] == 12, "a tall terminal has no reason to shrink the drawer"
    assert caps["queue"] == 3, "and no reason to collapse the queue"


def test_the_queue_keeps_its_rows_until_everything_else_has_given_way() -> None:
    """Tier 1: the queue collapses last.

    It is the one region holding durable state somebody is waiting on, so it
    gives up its rows only once the scrollable regions are at their floor.
    """
    caps = compact_caps(24, drawer_open=True, queue_items=3, turn_active=True)

    assert caps["queue"], "the queue collapsed while the drawer still had room"
    assert caps["drawer"] < 12, "the drawer did not give way first"


def test_a_region_that_cannot_fit_is_closed_rather_than_slivered() -> None:
    """Tier 1: below a usable height the drawer closes.

    A two-row drawer is not a smaller drawer — it is an unusable one that has
    also taken the conversation with it.
    """
    caps = compact_caps(16, drawer_open=True, queue_items=3, turn_active=True)

    assert caps["drawer"] == 0


def test_the_policy_never_asks_for_more_than_there_is() -> None:
    """Tier 1: across every height, the caps leave the two minimums intact.

    Swept rather than spot-checked: the failure this exists to stop was a
    combination nobody had tried, so asserting one combination would repeat
    the mistake.
    """
    for height in range(12, 41):
        for drawer in (False, True):
            for queued in (0, 3, 6):
                caps = compact_caps(
                    height,
                    drawer_open=drawer,
                    queue_items=queued,
                    turn_active=True,
                )
                asked = sum(caps.values())
                room = height - COMPOSER_MIN - FLOW_MIN
                assert asked <= room, (
                    f"at height {height} (drawer={drawer}, queued={queued}) the "
                    f"policy asked for {asked} rows with only {room} to give"
                )


# ── the wiring, on a real app ────────────────────────────────────────────────


async def _crowded(app, transport, pilot, *, drawer: bool = True):
    """Three queued, a turn running, and the Help drawer open — the owner's
    worst case and the one measured at one row of conversation."""
    for i in range(3):
        await transport.push_event(
            Event(
                type="user_submitted",
                data={
                    "text": f"queued {i}",
                    "chain_id": f"c{i}",
                    "msg_id": f"m{i}",
                    "seq": i + 1,
                    "meta": {},
                },
            )
        )
    await transport.push_event(
        Event(type="turn_started", data={"kind": "user", "chain_id": "cX", "seq": 99})
    )
    if drawer:
        app._open_drawer("help")
    for _ in range(8):
        await pilot.pause()


@pytest.mark.parametrize("height", [24, 20, 16])
@pytest.mark.asyncio
async def test_the_conversation_keeps_its_minimum_on_a_short_terminal(height) -> None:
    """Tier 2b: the acceptance condition, on the real app at three heights.

    80x24 is the figure #3680 names; the shorter two are where the same stack
    was worst (measured before: one row at 80x20, one at 80x16).
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, height)) as pilot:
        await pilot.pause()
        await _crowded(app, transport, pilot)

        flow = app.query_one(FlowView).size.height
        assert flow >= FLOW_MIN, (
            f"at 80x{height} the conversation was left {flow} rows with every "
            "region inside its own cap — the caps were individually correct "
            "and collectively wrong"
        )


@pytest.mark.asyncio
async def test_collapsing_the_queue_loses_no_item() -> None:
    """Tier 2b: the summary is a rendering, not a truncation.

    #3688 is the record of what a region silently showing less than it holds
    costs. The queue may stop spending a row per item; it may not stop having
    them.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        await _crowded(app, transport, pilot)

        queue = app.query_one(SentQueue)
        assert queue.summarised, "the queue did not collapse where it had to"
        assert queue.has_items(), "the queue reported itself empty once collapsed"
        assert "3" in " ".join(queue.rendered_texts()), (
            f"the summary does not say how many are waiting: {queue.rendered_texts()}"
        )


@pytest.mark.asyncio
async def test_re_deciding_the_layout_does_not_change_it() -> None:
    """Tier 2b: the decision is stable under repetition — it does not feed on
    its own result.

    ``_apply_compact_layout`` runs on a resize, on a drawer opening or closing,
    on every queued item arriving, and on the live chrome refresh. It asked the
    queue how many items it had via ``rendered_texts()`` — which, once
    collapsed, is ONE line however many are waiting. So collapsing made the
    count read 1, which said there was room, which expanded it, which made the
    count read 3 again: measured flipping on EVERY re-decide, which an operator
    sees as the region blinking between two layouts while they do nothing.

    Pinned by running the decision repeatedly rather than by asserting the
    fixed input, because the defect was not a wrong value — each pass was
    individually correct about what it could see. Only repetition shows it.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        await _crowded(app, transport, pilot)

        queue = app.query_one(SentQueue)
        settled = queue.summarised
        for attempt in range(4):
            app._apply_compact_layout()
            await pilot.pause()
            assert queue.summarised == settled, (
                f"re-decide #{attempt + 1} flipped the layout: {settled} -> "
                f"{queue.summarised}. The decision is reading its own output."
            )


@pytest.mark.asyncio
async def test_the_item_count_survives_being_collapsed() -> None:
    """Tier 2b: how many are queued does not change with how they are drawn.

    This is the property the oscillation violated, stated on its own so it
    holds even if the layout policy is rewritten: a caller asking "how many are
    waiting" must get the same answer collapsed or not. ``rendered_texts()``
    deliberately does not — it answers "what is on screen" — which is why the
    two are separate calls.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        await _crowded(app, transport, pilot)

        queue = app.query_one(SentQueue)
        assert queue.summarised, "this test needs the collapsed state to be the case"
        collapsed_count = queue.item_count()
        collapsed_lines = len(queue.rendered_texts())

        # Round-tripped through the widget rather than through a resize: at
        # THIS height closing the drawer is not enough room (the sibling test
        # above measures that), and the claim here is about the two reads, not
        # about the resize plumbing.
        queue.set_summarised(False)
        await pilot.pause()

        assert queue.item_count() == collapsed_count, (
            f"the item count moved with the rendering: {collapsed_count} "
            f"collapsed vs {queue.item_count()} expanded"
        )
        assert len(queue.rendered_texts()) != collapsed_lines, (
            "rendered_texts() did NOT change across the collapse, so this test "
            "would pass even if the two reads were the same call — the "
            "distinction it exists to pin is not being exercised"
        )


@pytest.mark.asyncio
async def test_room_returning_restores_the_rows() -> None:
    """Tier 2b: the policy is reversible.

    Everything it does is a rendering decision, so closing the thing that was
    competing has to put the queue's rows back — otherwise a moment of
    crowding would cost the operator their view for the rest of the session.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        await _crowded(app, transport, pilot)
        queue = app.query_one(SentQueue)
        assert queue.summarised

        # Closing the drawer is not enough at THIS height — the stack still
        # does not fit — which is itself the point: the collapse tracks the
        # room, not a latch that once set stays set. The policy says so for a
        # taller terminal, and the widget round-trips when told.
        assert compact_caps(34, queue_items=3, turn_active=True)["queue"] == 3, (
            "the policy would not restore the rows even given the room"
        )

        queue.set_summarised(False)
        await pilot.pause()
        assert not queue.summarised
        assert len(queue.rendered_texts()) == 3, (
            f"the rows did not come back: {queue.rendered_texts()}"
        )
