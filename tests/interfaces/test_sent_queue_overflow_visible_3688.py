"""#3688 — a queued item deeper than the sent-queue's height cap stays reachable.

The owner's report was "I sent a message while a reply was streaming and it
never appeared in the sent queue". Measurement located it here, not in the
delta path it looked like: the server emitted ``user_submitted`` 0.1 ms after
the submit, with a ``seq`` that the client's own ``RemoteQueueView`` gate
ACCEPTS — so the item was in the client's model and its row was mounted with
``display=True``. ``SentQueue`` capped its height at 6 rows with no
``overflow-y``, so row 7 onward was simply clipped off the bottom of the
screen. The six that survived were the OLDEST, which is why the row that
disappeared was always the one just submitted.

What these gates pin is the BEHAVIOUR ("an item past the cap is still
reachable on screen"), never the mechanism (no assertion on scroll offsets,
on the cap being 6, or on which rows happen to be visible at rest) — the cap
is a layout choice that may change; "nothing is silently hidden" is the
contract.

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` (the
``test_3300_p2b_sentqueue_render.py`` helper shape) — no ``unittest.mock``.
Visibility is read off what the compositor actually put on screen, because the
whole defect was invisible to every widget-level accessor: ``display`` was
True, ``rendered_texts()`` listed the row, and the model held the item.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (the ``test_3300_p2b_sentqueue_render.py`` helper shape)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

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


def _screen_text(app: TextualChatApp) -> str:
    """Everything the compositor actually painted — the only surface that can
    tell a clipped row from a drawn one."""
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    )


def _label(index: int) -> str:
    return f"QUEUED-{index:02d}"


async def _queue_n(transport: QueueTransport, pilot, count: int) -> None:
    for i in range(1, count + 1):
        await transport.push_event(
            Event(
                type="user_submitted",
                data={
                    "text": _label(i),
                    "chain_id": f"c{i}",
                    "msg_id": f"m{i}",
                    "seq": i,
                    "meta": {},
                },
            )
        )
    for _ in range(6):
        await pilot.pause()


@pytest.mark.asyncio
async def test_newest_queued_item_is_on_screen_past_the_height_cap() -> None:
    """Tier 2: the item submitted LAST is visible on screen even when the queue
    is deeper than the region's height cap.

    The owner-facing contract: what you just sent is what you most need to see.
    Before the fix this failed with nine rows in the model and the six OLDEST
    on screen.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _queue_n(transport, pilot, 9)

        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items()
        newest = _label(9)
        assert newest in _screen_text(app), (
            "the most recently queued item is not on screen — a submission that "
            "is in the model, mounted and display=True but clipped is "
            "indistinguishable, to the operator, from one the server dropped"
        )


@pytest.mark.asyncio
async def test_every_queued_item_is_reachable_by_navigating() -> None:
    """Tier 2: an item outside the visible window can be brought on screen by
    the region's own up/down selection.

    Enter cancels the selected item, so a selection that lands on a row nobody
    can see aims a destructive action at an invisible target. Navigating from
    the newest back to the oldest must surface each one.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _queue_n(transport, pilot, 9)

        sent_queue = app.query_one(SentQueue)
        sent_queue.focus()
        await pilot.pause()

        # Walk the whole queue; every row must become visible at some point.
        seen: "set[str]" = set()
        for _ in range(len(sent_queue.rendered_texts())):
            await pilot.pause()
            painted = _screen_text(app)
            seen.update(_label(i) for i in range(1, 10) if _label(i) in painted)
            sent_queue.action_select_next()
        for _ in range(len(sent_queue.rendered_texts())):
            await pilot.pause()
            painted = _screen_text(app)
            seen.update(_label(i) for i in range(1, 10) if _label(i) in painted)
            sent_queue.action_select_prev()
        await pilot.pause()
        seen.update(_label(i) for i in range(1, 10) if _label(i) in _screen_text(app))

        unreachable = sorted({_label(i) for i in range(1, 10)} - seen)
        assert not unreachable, (
            "queued items never became visible while navigating the region, so "
            f"Enter could cancel a row the operator cannot see: {unreachable}"
        )


@pytest.mark.asyncio
async def test_a_queue_within_the_cap_still_shows_every_item() -> None:
    """Tier 2: the fix does not disturb the ordinary case — a queue that fits
    shows all of its items, unchanged.

    Guards the direction the scroll change could plausibly break: making the
    region scrollable must not cost the small-queue rendering that already
    worked.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _queue_n(transport, pilot, 3)

        painted = _screen_text(app)
        missing = [_label(i) for i in range(1, 4) if _label(i) not in painted]
        assert not missing, f"a queue inside the cap lost rows: {missing}"
