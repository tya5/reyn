"""Owner request (relayed via lead-coder, 2026-08-07): "the blank line above
the input should move to directly below FlowView" — reason given: the
conversation and the NOW row (#3693) read as connected, which is hard to
read.

Measured BEFORE this fix: with a turn running, ``FlowView`` and
``ActivityRow`` were vertically ADJACENT (0-row gap) — the blank line lived
as ``#inputrow``'s own ``margin-top`` instead, between the transient chrome
(NOW row / search bar) and the input. That is backwards from what separates
conversation from status: the gap belongs to the conversation's own trailing
space, not the input's leading space.

The fix moves the gap onto ``FlowView``'s own ``margin-bottom`` — the ONE
region that is never collapsed (``display=False``), unlike every widget
between it and the composer (``ActivityRow``, ``SearchBar``, ``SentQueue``,
etc. all default to hidden). A margin on any of THOSE would vanish exactly
when that widget hides, which is the wrong direction for a gap whose whole
job is to stay put.

Three states are measured, but only TWO are gates on this fix: a turn
running (``ActivityRow`` visible, the owner's literal motivating case) and
the search bar open (#3692 PR-B ③'s ``ctrl+n``) both strip-falsify red
without the fix, because BEFORE it the gap sat on #inputrow's margin-top and
these chrome widgets were flush against FlowView. The idle case measures the
SAME gap=1 whether the fix is present or not — #inputrow's old margin-top
already covered it — so its test is a plain regression guard on the idle
case staying unchanged, not a witness of the fix (see its own docstring)."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import ActivityRow
from reyn.interfaces.inline.textual_chat.search_bar import SearchBar
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _Transport(ClientTransportStub):
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

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
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


def _gap(above, below) -> int:
    """Vertical rows between ``above``'s bottom and ``below``'s top — a
    margin (not part of either widget's own ``region``) reads as a positive
    gap here; two adjacent widgets read as 0."""
    return below.region.y - (above.region.y + above.region.height)


@pytest.mark.asyncio
async def test_idle_pane_still_has_a_gap_before_the_input() -> None:
    """Tier 2b: NOT a witness of this fix — idle measures gap=1 both before
    and after (stripping the fix here stays green, since #inputrow's own
    margin-top covered this exact case). This pins that the idle case is
    UNCHANGED by the ownership move, a plain regression guard, not one of
    the gates the fix itself depends on (those are the other two tests in
    this file, which strip-falsify red)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 24)) as pilot:
        app.query_one(Composer).focus()
        await pilot.pause()
        for i in range(6):
            app.conversation.append(OutboxMessage(kind="agent", text=f"reply {i}"))
        await pilot.pause()

        flow = app.query_one(FlowView)
        inputrow = app.query_one("#inputrow")
        assert _gap(flow, inputrow) == 1, (
            "the conversation-to-input gap did not survive moving off "
            "#inputrow's margin-top"
        )


@pytest.mark.asyncio
async def test_the_now_row_is_separated_from_the_conversation_not_the_input() -> None:
    """Tier 2b: the owner's literal motivating case. With a turn running,
    FlowView and ActivityRow (NOW) must NOT be adjacent — that is the
    "connected, hard to read" complaint this fix answers. The row is free to
    sit flush against the input; that pairing was never the complaint."""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(90, 24)) as pilot:
        app.query_one(Composer).focus()
        await pilot.pause()
        for i in range(6):
            app.conversation.append(OutboxMessage(kind="agent", text=f"reply {i}"))
        await pilot.pause()

        await transport.push_event(
            Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
        )
        await pilot.pause()

        flow = app.query_one(FlowView)
        row = app.query_one(ActivityRow)
        inputrow = app.query_one("#inputrow")
        assert row.display, "test setup: the NOW row did not appear for a running turn"
        assert _gap(flow, row) >= 1, (
            "the conversation pane and the NOW row are touching — this is "
            "exactly the 'connected, hard to read' complaint the fix answers"
        )
        assert _gap(row, inputrow) == 0, (
            "the NOW row grew unexpected space before the input; it was "
            "only ever supposed to give up its OWN adjacency to the input, "
            "not gain new adjacency to lose"
        )


@pytest.mark.asyncio
async def test_search_bar_open_still_separates_conversation_from_chrome() -> None:
    """Tier 2b: the other transient chrome region (#3692 PR-B ③'s
    ``ctrl+n``) — a second, independent witness that the gap is FlowView's
    own property, not tied to any one downstream widget's presence."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 24)) as pilot:
        app.query_one(Composer).focus()
        await pilot.pause()
        for i in range(6):
            app.conversation.append(OutboxMessage(kind="agent", text=f"reply {i}"))
        await pilot.pause()

        await pilot.press("ctrl+n")
        await pilot.pause()

        flow = app.query_one(FlowView)
        bar = app.query_one(SearchBar)
        inputrow = app.query_one("#inputrow")
        assert bar.display, "test setup: ctrl+n did not open the search bar"
        assert _gap(flow, bar) >= 1, (
            "the conversation pane and the search bar are touching"
        )
        assert _gap(bar, inputrow) == 0, (
            "the search bar grew unexpected space before the input"
        )
