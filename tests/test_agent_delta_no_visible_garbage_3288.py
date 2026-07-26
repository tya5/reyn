"""Tier 2: #3288 ③b — "no visible-garbage window" for the "agent_delta"
chat-event, witnessed on the ACTUAL ``TextualChatApp`` pump (production code
untouched — imported and driven read-only from this test, per the #3299/P5
non-interference constraint on ``interfaces/inline/textual_chat/``).

The owner's ratified design (issue #3288 comment thread) chose a chat-event
route specifically BECAUSE an EVENT frame with no consumer is dropped
(``_pump_frames``'s ``if/elif/.../continue`` has no ``else`` — consumed but
never drawn), whereas an unknown ``OutboxMessage`` DISPLAY kind is rendered as
a generic row by the presenter. ③c (a later phase, out of scope here) adds the
textual_chat coalescing consumer for "agent_delta"; UNTIL it lands, this test
is the CI gate that a future change cannot silently start drawing a row per
delta (a visible-garbage regression) without this test going RED — a
structural strip-style witness, not a one-time manual read of the source.

Real instances only: a real ``TextualChatApp`` driven via Textual's
``run_test()`` harness (mirrors ``tests/test_user_submitted_render_3300.py``'s
``QueueTransport`` idiom) and a real ``FlowView`` query — no mocks.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (mirrors ``tests/test_user_submitted_render_3300.py``'s helper of
    the same name) — lets a test push a frame and inspect
    ``TextualChatApp``'s retained conversation model afterward."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

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


@pytest.mark.asyncio
async def test_agent_delta_event_draws_nothing_in_textual_chat() -> None:
    """Tier 2: pushing an "agent_delta" EVENT frame through the REAL
    ``TextualChatApp._pump_frames`` must not append a FlowView entry — the
    event is consumed (the pump does not crash / stall) but not drawn, since
    no branch in the (untouched) pump matches "agent_delta" yet.

    Strip-falsify (recorded in the PR body): re-emitting the SAME delta as a
    ``DisplayFrame`` (an ``OutboxMessage``-kind carrier — the design the
    owner's decision REPLACES) through this same app DOES append a generic
    row — proving the chat-event route is what closes the visible-garbage
    window, not an accident of this particular event's payload shape.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(app.query_one(FlowView).entries)

        await transport.push_event(
            Event(type="agent_delta", data={"text": "partial chunk", "chain_id": "c1"})
        )
        await pilot.pause()

        after = len(app.query_one(FlowView).entries)
        assert after == before, (
            "an agent_delta chat-event must draw NOTHING (opt-in draw, no "
            f"consumer yet) — entry count changed {before} -> {after}"
        )


@pytest.mark.asyncio
async def test_agent_delta_ignored_alongside_other_unhandled_events() -> None:
    """Tier 2: non-vacuity companion — a DIFFERENT unhandled event type
    (mirroring ``test_textual_chat_app_ignores_non_user_submitted_events_for_flow``)
    also draws nothing, proving the previous test's zero-delta is because NO
    unhandled EVENT frame draws anything (the pump's structural behavior),
    not a coincidence specific to the "agent_delta" payload shape."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(app.query_one(FlowView).entries)

        await transport.push_event(Event(type="some_future_unhandled_event", data={}))
        await pilot.pause()

        after = len(app.query_one(FlowView).entries)
        assert after == before
