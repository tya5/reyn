"""#3490 — the addressed row is marked by a GUTTER RAIL, not a style overlay.

#3476 ⑤/⑥ marked the search hit / keyboard cursor with
``text-style: reverse`` on flowview's ``--selected``/``--cursor`` component
classes. That survived flowview's style merge but inverted fg/bg into a
near-white block over the palette (owner review: it broke the design). The
mark is now CONTENT — a thin rail in a gutter (the right one since #3526) —
which
is what makes it work uniformly:

flowview applies a component style as ``Segment.apply_style(segments, style)``
== ``style + segment.style``, i.e. as a BASE beneath each segment's own
attributes, and passes no ``post_style``. So a component *background* is
swallowed on every row whose presentation carries a full-row
``Presentation.background`` — the user's own line and any failure row. These
tests therefore assert the rail on BOTH a plain agent row and a backdropped
user row, and assert the absence of reverse video, because "it survives the
merge" and "it looks right" are two different requirements and only the
second one failed last time.

Read off the PAINTED surface (``FlowView.render_line`` — the same strips a
terminal receives) with a real ``TextualChatApp`` + a real minimal
``ClientTransport``: no mocks, no private view state.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.gutter import _MARK_RAIL
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

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


def _painted_rows(flow: FlowView) -> "list[str]":
    """Every visible row of the pane, as painted (gutter included)."""
    return [
        "".join(segment.text for segment in flow.render_line(y))
        for y in range(flow.size.height)
    ]


def _railed_rows(flow: FlowView) -> "list[str]":
    return [row for row in _painted_rows(flow) if _MARK_RAIL in row]


def _reversed_rows(flow: FlowView) -> "list[str]":
    """Rows painted with reverse video — the regression this issue is about."""
    out = []
    for y in range(flow.size.height):
        strip = flow.render_line(y)
        if any(seg.style is not None and seg.style.reverse for seg in strip):
            out.append("".join(seg.text for seg in strip))
    return out


async def _focus_flow(pilot, app) -> "FlowView":
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    flow = app.query_one(FlowView)
    assert app.focused is flow, f"setup: Shift+Tab did not focus FlowView: {app.focused!r}"
    return flow


@pytest.mark.asyncio
async def test_the_cursor_row_is_railed_and_nothing_is_reverse_video() -> None:
    """Tier 2b: the keyboard cursor's row paints a rail — and NO row paints
    reverse video (the near-white inversion #3476 ⑥ shipped)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("older reply", "newest reply"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)

        railed = _railed_rows(flow)
        assert any("newest reply" in row for row in railed), (
            f"the cursor row is not railed: {railed!r}"
        )
        assert not any("older reply" in row for row in railed), (
            f"a row the cursor is not on is railed: {railed!r}"
        )
        assert _reversed_rows(flow) == [], (
            f"reverse video is painted again: {_reversed_rows(flow)!r}"
        )


@pytest.mark.asyncio
async def test_a_backdropped_user_row_is_railed_too() -> None:
    """Tier 2b: the rail shows on the USER's own row — the row kind whose
    full-row ``Presentation.background`` swallows any component-style
    background, and the reason the mark cannot be a style overlay."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply"))
        app.conversation.append(OutboxMessage(kind="user", text="my own question"))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)

        railed = _railed_rows(flow)
        assert any("my own question" in row for row in railed), (
            f"the user row is not railed: {railed!r}"
        )
        assert not any("a reply" in row for row in railed), (
            f"the rail leaked onto the agent row: {railed!r}"
        )


@pytest.mark.asyncio
async def test_the_rail_moves_with_the_cursor_leaving_no_trail() -> None:
    """Tier 2b: moving the cursor repaints the row it LEFT as well as the one
    it arrived on — the gutter cache is keyed on a decor revision a cursor
    move does not bump, so a missing invalidation would strand the rail."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("first", "second", "third"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)
        assert any("third" in row for row in _railed_rows(flow)), (
            "setup: the rail did not start on the last row"
        )

        await pilot.press("up")
        await pilot.pause()
        railed = _railed_rows(flow)
        assert any("second" in row for row in railed), (
            f"the rail did not follow the cursor: {railed!r}"
        )
        assert not any("third" in row for row in railed), (
            f"the rail was left behind on the row the cursor left: {railed!r}"
        )


@pytest.mark.asyncio
async def test_the_rail_spans_a_multi_row_entrys_full_height() -> None:
    """Tier 2b: a wrapped, multi-row body is marked down its WHOLE height, so
    one entry reads as one marked block rather than a mark on its first line."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        long_text = " ".join(f"word{i}" for i in range(60))  # wraps at width 60
        app.conversation.append(OutboxMessage(kind="agent", text=long_text))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)

        railed = _railed_rows(flow)
        # ``word0`` is on the body's FIRST painted row, ``word59`` on its LAST:
        # both railed == the mark spans the whole entry, not only its head.
        assert any("word0 " in row for row in railed), (
            f"the entry's first row is not railed: {railed!r}"
        )
        assert any("word59" in row for row in railed), (
            f"the entry's last row is not railed — the rail stopped short: {railed!r}"
        )
        assert _reversed_rows(flow) == []


@pytest.mark.asyncio
async def test_an_unaddressed_pane_paints_no_rail() -> None:
    """Tier 2b: with focus on the composer and no search running, nothing is
    addressed — the pane paints no rail at all (the mark is an affordance for
    an active position, not permanent chrome)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)
        assert _railed_rows(flow) == [], f"an unaddressed pane is railed: {_railed_rows(flow)!r}"


@pytest.mark.asyncio
async def test_leaving_the_pane_hides_the_rail_but_keeps_the_position() -> None:
    """Tier 2b: Esc back to the composer removes the rail — nothing is being
    addressed, and a rail on an unfocused pane is permanent chrome rather than
    an affordance. Re-entering the pane brings it back on the SAME row, so the
    position is remembered even though the mark is not painted meanwhile."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("first", "second"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)
        await pilot.press("up")
        await pilot.pause()
        assert any("first" in row for row in _railed_rows(flow)), (
            "setup: the cursor was not moved onto the older row"
        )

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Composer).has_focus, "setup: Esc did not reach the composer"
        assert _railed_rows(flow) == [], (
            f"the rail survived leaving the pane: {_railed_rows(flow)!r}"
        )

        await pilot.press("shift+tab")
        await pilot.pause()
        assert any("first" in row for row in _railed_rows(flow)), (
            f"re-entering did not restore the rail on the remembered row: "
            f"{_railed_rows(flow)!r}"
        )


@pytest.mark.asyncio
async def test_closing_search_removes_the_hits_rail() -> None:
    """Tier 2b: Esc out of the search bar takes the hit's rail with it — the
    search-hit leg is gated on the bar being open, mirroring the cursor leg's
    focus gate."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="alpha match"))
        await pilot.pause()
        await pilot.press("ctrl+n")
        for ch in "alpha":
            await pilot.press(ch)
        await pilot.pause()
        flow = app.query_one(FlowView)
        assert _railed_rows(flow), "setup: the search hit was not railed"

        await pilot.press("escape")
        await pilot.pause()
        assert _railed_rows(flow) == [], (
            f"the hit's rail survived closing the search bar: {_railed_rows(flow)!r}"
        )


@pytest.mark.asyncio
async def test_the_search_hit_is_railed() -> None:
    """Tier 2b: the ⑤ search hit shares the SAME rail as the cursor — both mean
    "the row you are addressing", so they are one vocabulary, not two."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("alpha match", "unrelated"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()

        await pilot.press("ctrl+n")
        for ch in "alpha":
            await pilot.press(ch)
        await pilot.pause()

        flow = app.query_one(FlowView)
        railed = _railed_rows(flow)
        assert any("alpha match" in row for row in railed), (
            f"the search hit is not railed: {railed!r}"
        )
        assert not any("unrelated" in row for row in railed), (
            f"a non-matching row is railed: {railed!r}"
        )
        assert _reversed_rows(flow) == []


@pytest.mark.asyncio
async def test_only_one_entry_is_ever_railed_even_with_search_open() -> None:
    """Tier 2b: #3493 — two DIFFERENT rows can never both be railed.

    The reachable case: open search (the hit becomes the addressed row), then
    ``Shift+Tab`` into the pane while the bar is STILL open. Before #3493 the
    cursor and a separate search selection were two independent positions
    sharing one rail, so this painted a rail on each and the mark stopped
    meaning "the row you are on". Search now moves the CURSOR, so there is one
    position by construction rather than by a gating rule that has to stay
    correct."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("alpha match", "middle row", "newest row"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()

        await pilot.press("ctrl+n")
        for ch in "alpha":
            await pilot.press(ch)
        await pilot.pause()
        flow = app.query_one(FlowView)
        assert any("alpha match" in row for row in _railed_rows(flow)), (
            "setup: the search hit was not railed"
        )

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is flow, "setup: Shift+Tab did not reach the pane"
        assert app.query_one("#search-bar").display, (
            "setup: the search bar closed, so the two-position case is not exercised"
        )

        railed = _railed_rows(flow)
        assert not any("newest row" in row for row in railed), (
            f"a second row is railed alongside the hit: {railed!r}"
        )
        assert any("alpha match" in row for row in railed), (
            f"the addressed row lost its rail: {railed!r}"
        )


def _row_backgrounds(flow: FlowView) -> "dict[str, set]":
    """Painted background colours per visible row, keyed by the row's text."""
    out = {}
    for y in range(flow.size.height):
        strip = flow.render_line(y)
        text = "".join(seg.text for seg in strip).strip()
        if not text:
            continue
        out[text] = {
            str(seg.style.bgcolor)
            for seg in strip
            if seg.style is not None and seg.style.bgcolor is not None
        }
    return out


@pytest.mark.asyncio
async def test_the_addressed_row_keeps_its_own_background() -> None:
    """Tier 2b: #3496 — being addressed changes NOTHING about the row's own
    colours; the mark is the gutter cell and nothing else.

    Owner review found the opposite twice. First as reverse video, then — after
    the CSS rule was merely REMOVED — as a near-black block ("一番下のエントリ
    は常に真っ黒背景"; the cursor auto-arms on the newest entry, so the bottom
    row wore it permanently). Root cause measured: Textual resolves an
    UNDECLARED component class to a concrete style synthesised from inherited
    values (``Style(color=#e0e0e0, bgcolor=#121212)``), and flowview applies
    that to the addressed row — so "declare no rule" is NOT "paint nothing",
    and neither is ``background: transparent``. ``_UnmarkedFlowView`` suppresses
    the accessor instead.

    This asserts the property the earlier tests missed: they pinned the absence
    of REVERSE and the presence of the rail, never that the row's background
    was left alone."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("untouched row", "addressed row"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        flow = app.query_one(FlowView)
        before = _row_backgrounds(flow)

        await _focus_flow(pilot, app)
        after = _row_backgrounds(flow)

        addressed = next(k for k in after if "addressed row" in k)
        assert any(_MARK_RAIL in k for k in after), (
            "setup: nothing is railed, so the addressed state is not exercised"
        )
        assert after[addressed] == before["● addressed row"], (
            "being addressed changed the row's background: "
            f"{before['● addressed row']!r} -> {after[addressed]!r}"
        )
        assert after["● untouched row"] == before["● untouched row"], (
            "a row that is NOT addressed changed too"
        )
