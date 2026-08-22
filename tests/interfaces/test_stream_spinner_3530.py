"""#3530 — a reply still receiving chunks blinks; the terminal frame stops it.

A streamed reply's prose stops the same way whether the model paused or
finished, so the text alone cannot answer "is more coming?". The gutter marker
answers it: it animates while the stream is open and goes still when the end
arrives.

★ The state is READ, never inferred. ``_streaming_replies`` holds a record from
the first delta until the TERMINAL COMPLETION FRAME pops it, so a long pause
mid-reply keeps blinking — which is the case the owner asked about, and the
case a "quiet for N seconds means done" rule would get wrong precisely when the
wait is longest. One test below is dedicated to that invariant.

The clock is injected through the app's own ``clock`` parameter (production
passes ``time.monotonic``), so the blink is driven rather than slept through.
"""
from __future__ import annotations

import asyncio
import types
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.gutter import (
    _RUNNING_FRAME_PERIOD,
    _RUNNING_FRAMES,
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


class _DrivenClock:
    """A real callable standing in for ``time.monotonic`` — the value only moves
    when a test moves it, so a blink can be observed without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _gutter_markers(flow: FlowView) -> "list[str]":
    """The leading gutter glyph of every non-blank painted row."""
    markers = []
    for y in range(flow.size.height):
        row = "".join(segment.text for segment in flow.render_line(y))
        if row.strip():
            markers.append(row[:2].strip())
    return markers


async def _markers_over_one_blink(pilot, app, flow, clock) -> "set[str]":
    """Every gutter marker painted across a full frame period."""
    seen = set()
    for _ in range(4):
        seen.update(_gutter_markers(flow))
        clock.advance(_RUNNING_FRAME_PERIOD)
        flow._tick_animation()
        await pilot.pause()
    return seen


def _delta(chain_id: str, text: str):
    return types.SimpleNamespace(data={"chain_id": chain_id, "text": text})


@pytest.mark.asyncio
async def test_a_reply_still_receiving_chunks_blinks() -> None:
    """Tier 2b: an open stream animates its marker."""
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._handle_agent_delta_event(_delta("c1", "partial reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)

        seen = await _markers_over_one_blink(pilot, app, flow, clock)
        assert seen >= set(_RUNNING_FRAMES), (
            "the streaming row did not cycle through the animation frames "
            f"{_RUNNING_FRAMES!r}; it painted {seen!r}"
        )


@pytest.mark.asyncio
async def test_the_terminal_frame_stops_the_blink() -> None:
    """Tier 2b: arrival of the end is what settles the marker.

    Asserted after the same number of ticks that made it blink above, so a
    still marker here means the stream closed, not that the clock stood still.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._handle_agent_delta_event(_delta("c2", "partial reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)
        assert await _markers_over_one_blink(pilot, app, flow, clock) >= set(
            _RUNNING_FRAMES
        ), "setup: the row was not blinking before the terminal frame"

        app._ingest_frame(
            OutboxMessage(kind="agent", text="the whole reply", meta={"chain_id": "c2"})
        )
        await pilot.pause()

        seen = await _markers_over_one_blink(pilot, app, flow, clock)
        assert seen == {"●"}, (
            f"the marker kept animating after the reply ended: {seen!r}"
        )


@pytest.mark.asyncio
async def test_a_pause_between_chunks_does_not_settle_the_marker() -> None:
    """Tier 2b: silence is not an ending. ★

    The anti-heuristic invariant. Time is advanced far past any plausible
    idle-timeout with NO terminal frame, and the marker must still be moving —
    a model thinking for twenty seconds is the exact case the owner wants the
    spinner for, and a timeout rule would report it as finished.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._handle_agent_delta_event(_delta("c3", "thinking"))
        await pilot.pause()
        flow = app.query_one(FlowView)

        clock.advance(600.0)  # ten minutes of silence, no terminal frame
        flow._tick_animation()
        await pilot.pause()

        seen = await _markers_over_one_blink(pilot, app, flow, clock)
        assert seen >= set(_RUNNING_FRAMES), (
            "a long gap between chunks settled the marker — the spinner is "
            f"reading elapsed time rather than the stream's own state: {seen!r}"
        )


@pytest.mark.asyncio
async def test_a_reply_that_never_streamed_does_not_blink() -> None:
    """Tier 2b: the animation belongs to streaming, not to agent rows.

    A reply delivered whole (no deltas) is already complete when it lands, so
    animating it would say "more is coming" about something finished.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a whole reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)

        seen = await _markers_over_one_blink(pilot, app, flow, clock)
        assert seen == {"●"}, f"a non-streamed reply animated its marker: {seen!r}"


@pytest.mark.asyncio
async def test_the_streaming_marker_does_not_borrow_the_running_colour() -> None:
    """Tier 2b: blinking says "working", amber says "needs you".

    The gutter's amber is its at-risk/attention vocabulary (`EntryState.RUNNING`
    on tool rows). A reply arriving normally is neither, so it keeps its own
    kind colour and only the MOTION carries the meaning — otherwise a healthy
    stream would look like something demanding the reader.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="settled reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)

        def marker_colour(y: int) -> str:
            return str(next(iter(flow.render_line(y))).style.color)

        settled_row = next(
            y
            for y in range(flow.size.height)
            if "settled reply" in "".join(s.text for s in flow.render_line(y))
        )
        settled_colour = marker_colour(settled_row)

        app._handle_agent_delta_event(_delta("c4", "streaming reply"))
        await pilot.pause()
        streaming_row = next(
            y
            for y in range(flow.size.height)
            if "streaming reply" in "".join(s.text for s in flow.render_line(y))
        )
        streaming_colour = marker_colour(streaming_row)

        assert streaming_colour == settled_colour, (
            "the streaming marker changed colour as well as moving "
            f"({settled_colour} -> {streaming_colour}) — motion alone is the cue"
        )


# ── a stream ends in more ways than one ────────────────────────────────────
#
# #3530 read "still streaming" off `_streaming_replies` and blinked on it,
# which is right — but it treated the TERMINAL COMPLETION FRAME as the only way
# that state ends. `Ctrl+C` cancels through the transport without producing one,
# so the record survived and the marker blinked forever (owner report,
# 2026-08-02, reproduced below).
#
# The fix is not "also handle cancel". Every way a stream can stop — a terminal
# frame, a cancel, an error, a dropped connection — surfaces as one of the three
# TURN-END events, which is where the tool-row sweep already lives. Releasing
# both maps at that one boundary is a property of the turn rather than a list of
# causes that the next unlisted cause would escape.


@pytest.mark.asyncio
async def test_a_cancelled_stream_stops_blinking() -> None:
    """Tier 2b: the marker settles when the turn ends without a terminal frame.

    Driven through the turn-end sweep rather than by clearing the map directly:
    the defect was never in what the blink reads, it was in nothing telling it
    the wait was over.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._handle_agent_delta_event(_delta("c9", "half a reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)

        assert await _markers_over_one_blink(pilot, app, flow, clock) >= set(
            _RUNNING_FRAMES
        ), "setup: the row was not blinking before the turn ended"

        app._sweep_orphaned_streaming_replies()
        await pilot.pause()

        assert await _markers_over_one_blink(pilot, app, flow, clock) == {"●"}, (
            "the marker kept animating after the turn ended without a terminal "
            "frame — a cancelled stream is still waiting for chunks that will "
            "never arrive"
        )


@pytest.mark.asyncio
async def test_the_cancelled_reply_keeps_the_text_it_received() -> None:
    """Tier 2b: settling the marker does not discard what already arrived.

    The cheap fix would be to drop the entry along with the record. What the
    user typed for and half-received is theirs — only the "more is coming"
    claim is withdrawn.
    """
    clock = _DrivenClock()
    app = TextualChatApp(transport=_Transport(), clock=clock)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app._handle_agent_delta_event(_delta("c10", "the part that did arrive"))
        await pilot.pause()

        app._sweep_orphaned_streaming_replies()
        await pilot.pause()

        flow = app.query_one(FlowView)
        painted = "\n".join(
            "".join(s.text for s in flow.render_line(y))
            for y in range(flow.size.height)
        )
        assert "the part that did arrive" in painted


@pytest.mark.asyncio
async def test_every_turn_end_event_releases_the_stream() -> None:
    """Tier 2b: the release rides the turn boundary, not one named cause.

    Asserted across all three turn-end events because the defect was a missing
    CAUSE, and a fix pinned to the one cause that was reported would leave the
    others exactly as broken.
    """
    from reyn.interfaces.inline.textual_chat.app import _TURN_END_EVENT_TYPES

    assert _TURN_END_EVENT_TYPES, "setup: no turn-end events to check"

    for end_event in sorted(_TURN_END_EVENT_TYPES):
        clock = _DrivenClock()
        app = TextualChatApp(transport=_Transport(), clock=clock)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            app._handle_agent_delta_event(_delta("c11", "partial"))
            await pilot.pause()
            flow = app.query_one(FlowView)

            app._sweep_orphaned_streaming_replies()
            await pilot.pause()

            assert await _markers_over_one_blink(pilot, app, flow, clock) == {"●"}, (
                f"the stream stayed marked in-flight after {end_event}"
            )
