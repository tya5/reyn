"""#3526 — the addressed-row rail is drawn in the RIGHT gutter, not the left.

The rail marks the keyboard cursor's row. It lived in the LEFT gutter's trailing
cell from #3490 until the owner asked for it on the other side.

These tests pin the SIDE, which is the thing #3490's own suite cannot see: those
15 tests ask ``_MARK_RAIL in row`` and so passed unchanged when the rail moved
from one edge of the pane to the other. A property that survives its own
negation is not being tested, so the column is asserted here explicitly.

The second thing asserted is what the move must NOT disturb: the left gutter
gets its trailing cell back, and reclaiming it must not shift the body.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.gutter import (
    _MARK_RAIL,
    RIGHT_GUTTER_WIDTH,
)
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

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

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


def _rows(flow: FlowView) -> "list[str]":
    return [
        "".join(seg.text for seg in flow.render_line(y))
        for y in range(flow.size.height)
    ]


async def _address_the_pane(pilot, app) -> FlowView:
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    return app.query_one(FlowView)


@pytest.mark.asyncio
async def test_the_rail_is_drawn_in_the_right_gutters_leading_cell() -> None:
    """Tier 2b: the rail's column is the first cell of the right gutter.

    Asserted as a COLUMN rather than "the glyph is somewhere on the row",
    because the latter is exactly what stayed green while the rail sat on the
    opposite edge of the pane.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply"))
        await pilot.pause()
        flow = await _address_the_pane(pilot, app)

        railed = [row for row in _rows(flow) if _MARK_RAIL in row]
        assert railed, "the addressed row was not railed at all"

        expected = flow.size.width - RIGHT_GUTTER_WIDTH
        for row in railed:
            assert row.index(_MARK_RAIL) == expected, (
                f"the rail is at column {row.index(_MARK_RAIL)}, not the right "
                f"gutter's leading cell ({expected}); row: {row!r}"
            )


@pytest.mark.asyncio
async def test_marking_a_row_does_not_shift_its_body() -> None:
    """Tier 2b: the left gutter reclaimed its trailing cell without moving text.

    The rail used to occupy that cell, so the body's start column is the place a
    botched reclaim would show up — and it would show up as the whole pane
    twitching one column whenever the cursor lands, which no assertion about the
    rail itself can catch.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="marker-text"))
        await pilot.pause()
        flow = app.query_one(FlowView)
        unmarked = next(row for row in _rows(flow) if "marker-text" in row)

        await _address_the_pane(pilot, app)
        marked = next(row for row in _rows(flow) if "marker-text" in row)

        assert _MARK_RAIL in marked, "setup: the row was not addressed"
        assert marked.index("marker-text") == unmarked.index("marker-text"), (
            "addressing the row shifted its body horizontally: "
            f"{unmarked!r} -> {marked!r}"
        )


@pytest.mark.asyncio
async def test_the_rail_spans_a_wrapped_entry_at_one_column() -> None:
    """Tier 2b: a multi-row reply reads as ONE marked block on the new side.

    Every row of the entry carries the rail and they all line up — a rail that
    only marked the first row, or that stepped across columns as the right
    gutter's labels came and went, would read as several marks rather than one
    block.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.pause()
        app.conversation.append(
            OutboxMessage(kind="agent", text=" ".join(f"word{i}" for i in range(60)))
        )
        await pilot.pause()
        flow = await _address_the_pane(pilot, app)

        railed = [row for row in _rows(flow) if _MARK_RAIL in row]
        assert any("word0 " in row for row in railed), (
            f"the entry's first row was not railed: {railed!r}"
        )
        assert any("word59" in row for row in railed), (
            f"the entry's last row was not railed — the mark stopped short of "
            f"the end of the reply: {railed!r}"
        )
        assert len({row.index(_MARK_RAIL) for row in railed}) == 1, (
            f"the rail did not hold a single column down the entry: {railed!r}"
        )
