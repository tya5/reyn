"""#3693 — the live-turn line above the composer.

The RUNNING gutter is the right expression of a live turn inside the
conversation, and it scrolls away. This region answers "is the turn still
alive, and what is it doing" next to the composer, for the duration of the
turn only.

Every gate here is about NOT FABRICATING. The row may say only what the client
observed: a turn is running (``turn_active``), content is arriving
(``agent_delta``), a named tool is in flight (a labelled frame). A client that
attached mid-turn knows the first and none of the rest, and must not print an
elapsed time measured from when it happened to connect.

#3777 (clean break): the ``NEXT`` label that used to single out the head of
the sent-queue is gone (owner call: no special-case for the head row at all,
"option ①") — the three tests that asserted its presence/positioning/hand-off
were retired in the same PR that removed the feature (the surviving
queue-order/promotion-once properties they incidentally exercised are already
covered by ``test_3300_p2b_sentqueue_render.py``'s own tests, which do not
depend on NEXT). See the "the shine" section below for the glyph #3777 put on
THIS row instead.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.content import Span

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import (
    _SHINE_WIDTH,
    ActivityRow,
    activity_text,
)
from reyn.interfaces.inline.textual_chat.sent_queue import ROW_TEXT_COLUMN
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


def _started(chain_id: str, seq: int) -> Event:
    return Event(type="turn_started", data={"kind": "user", "chain_id": chain_id, "seq": seq})


async def _settle(pilot, times: int = 4) -> None:
    for _ in range(times):
        await pilot.pause()


async def _until(pred) -> None:
    """Wait for ``pred()`` to become true — UNBOUNDED, no per-test time
    budget (owner's testing policy, docs/deep-dives/contributing/testing.md
    § Time: a test carries no time limit of its own, marker or in-body; a
    slower environment must only make this slower, never fail it — an
    ``attempts=N`` cap is a disguised linear sleep, since past N it bets
    pass/fail on elapsed time the same way a bare ``sleep(N)`` would). If
    this hangs, CI's ``--timeout=120`` is the blast-radius kill-switch, not
    a contract this test is written against — a hang there means "decompose
    this test or fix the hang," never "the ceiling should have been bigger."

    Used here (see ``test_the_clock_advances_without_any_delta_arriving``)
    to poll for the OBSERVABLE effect of the row's own real ``set_interval``
    timer firing — this still genuinely requires that real timer to have
    fired at least once; it never calls ``tick()`` directly (which would
    pass even if ``on_mount``'s ``set_interval`` call were silently removed,
    the exact regression this test exists to catch)."""
    while not pred():
        await asyncio.sleep(0.01)


# ── the row's own text: what it may and may not claim ────────────────────────


def test_an_unknown_duration_prints_no_clock() -> None:
    """Tier 1: no elapsed time is invented when none was observed.

    A client that joined mid-turn has no start instant. "00:00" would read as a
    measurement; printing nothing reads as what it is.
    """
    with_clock = str(activity_text("WORKING", elapsed_s=78.0, width=80))
    without = str(activity_text("WORKING", elapsed_s=None, width=80))

    assert "01:18" in with_clock
    assert not any(ch.isdigit() for ch in without.split("Ctrl+C")[0]), (
        f"a duration appeared where none was known: {without!r}"
    )


def test_the_cancel_affordance_yields_before_the_state_does() -> None:
    """Tier 1: on a narrow row the state survives and the hint is what goes.

    Both cannot fit at every width. The state is the thing the row exists to
    show; the hint names a key that works whether or not it is printed.
    """
    wide = str(activity_text("RESPONDING", elapsed_s=5.0, width=80))
    narrow = str(activity_text("RESPONDING", elapsed_s=5.0, width=20))

    assert "Ctrl+C" in wide
    assert "RESPONDING" in narrow
    assert len(narrow) <= len(wide)


# ── the lifecycle, on a real app ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_row_is_absent_until_a_turn_runs_and_gone_after_it() -> None:
    """Tier 2b: hidden while idle, shown while a turn runs, hidden again.

    The whole point is that its presence means something. A row that stayed up
    between turns would say "a turn is running" when none is.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        row = app.query_one(ActivityRow)
        assert row.display is False

        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        assert row.display is True
        assert row.state == "WORKING"

        await transport.push_event(Event(type="turn_completed", data={"chain_id": "c1"}))
        await _settle(pilot)
        assert row.display is False
        assert row.state is None


@pytest.mark.asyncio
async def test_content_arriving_specialises_the_state() -> None:
    """Tier 2b: deltas move the row from WORKING to RESPONDING.

    Asserted through a real ``agent_delta`` rather than by calling the widget,
    so this fails if the wiring is removed and the widget still works.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        assert app.query_one(ActivityRow).state == "WORKING"

        await transport.push_event(
            Event(type="agent_delta", data={"chain_id": "c1", "text": "hello"})
        )
        await _settle(pilot)

        assert app.query_one(ActivityRow).state == "RESPONDING"


@pytest.mark.asyncio
async def test_a_delta_outside_a_turn_creates_no_row() -> None:
    """Tier 2b: a stray delta does not conjure a live-turn row.

    The row's claim is "a turn is running". Only a dispatch may make that
    claim; every other frame may refine it.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await transport.push_event(
            Event(type="agent_delta", data={"chain_id": "orphan", "text": "hi"})
        )
        await _settle(pilot)

        row = app.query_one(ActivityRow)
        assert row.display is False
        assert row.state is None


@pytest.mark.asyncio
async def test_every_terminal_event_clears_the_row() -> None:
    """Tier 2b: settled, completed and cancelled all end it.

    A turn can finish three ways. Covering one and assuming the rest is how a
    row survives its own turn — and a stuck "RESPONDING" is worse than none,
    because it is confidently wrong.
    """
    for terminal in ("turn_settled", "turn_completed", "turn_cancelled"):
        transport = QueueTransport()
        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await transport.push_event(_started("c1", 1))
            await _settle(pilot)
            assert app.query_one(ActivityRow).display is True

            await transport.push_event(Event(type=terminal, data={"chain_id": "c1"}))
            await _settle(pilot)

            assert app.query_one(ActivityRow).display is False, (
                f"the row survived {terminal}"
            )


@pytest.mark.asyncio
async def test_the_row_is_not_focusable() -> None:
    """Tier 2b: it never takes a stop on the way back to the composer.

    The queue below owns selection and cancel; Esc returns to typing. A
    focusable row here would add a place to be stuck between them.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)

        assert app.query_one(ActivityRow).focusable is False


# #3777 (clean break, CLAUDE.md testing.md § extracted-refactor test
# lifecycle): the NEXT-label section that used to sit here — three tests
# asserting the head-of-queue label's presence, exclusivity, and hand-off —
# was retired with the label itself (owner call, option ①: no special-case
# for the head row). The queue-order and promotion-once properties those
# tests incidentally exercised are covered independently of NEXT by
# ``test_3300_p2b_sentqueue_render.py::test_turn_started_promotes_matching_item_to_flow_entry``.


@pytest.mark.asyncio
async def test_the_clock_advances_without_any_delta_arriving() -> None:
    """Tier 2b: the elapsed time moves on its own schedule, not on traffic.

    The first version had no timer: the clock was redrawn only as a side effect
    of a delta refining the state. So through a tool call, or after the stream
    ended, the row kept printing a number that had stopped being true while
    still looking live — the same failure as printing an invented one, which
    this row exists not to do.

    Deliberately driven with NO frames at all. A gate that pushed deltas would
    go green on the side-effect path this exists to replace.

    ★ #3746-shaped fix: a fixed ``asyncio.sleep(TICK_SECONDS * 1.4)`` waits on
    the row's real ``set_interval`` timer (``activity_row.py``, real
    wall-clock, NOT the injected ``clock`` above — same mechanism #3746 found
    in ``app.py``'s streaming catch-up timer) with a FIXED margin, which a
    loaded CI runner can exceed. Unlike #3746's coalesce test, this one CANNOT
    simply disable the real timer — the timer firing on its own schedule IS
    the property under test (the docstring's own point: a gate driven by
    calling ``tick()`` directly would go green on the exact side-effect path
    this test exists to replace). Fixed by polling for the OBSERVABLE effect
    (the render text actually changing) with a generous bounded ceiling
    instead of a single fixed sleep — still requires the real timer to have
    fired, just tolerant of how long that takes under load.
    """
    ticks: "list[float]" = [1000.0]
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: ticks[0])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        row = app.query_one(ActivityRow)
        before = str(row.render())

        # Time passes; nothing arrives. Unbounded wait (see `_until`): if the
        # elapsed time never moves, this hangs rather than failing with a
        # message — CI's own kill-switch is what surfaces that, not a local
        # budget pretending to know how long is "too long."
        ticks[0] += 75.0
        await _until(lambda: str(row.render()) != before)

        after = str(row.render())
        assert "01:15" in after, f"the clock is not tracking the real gap: {after!r}"


# ── the shine (owner design "A", replacing the removed NOW label) ────────────


def test_the_row_carries_no_mark_and_starts_where_a_queue_label_starts() -> None:
    """Tier 1: no ``NOW`` label and no glyph — the row opens with the indent
    that puts its text in the same column a queue row's LABEL occupies.

    The alignment is the claim, so it is checked against the queue's own
    constant rather than against a hard-coded three: a queue that changed its
    glyph gap and left this row behind is exactly the drift the shared
    constant exists to prevent, and a literal here would keep passing through
    it.
    """
    rendered = str(activity_text("WORKING", elapsed_s=None, width=80))
    assert "NOW" not in rendered
    assert rendered.startswith(" " * ROW_TEXT_COLUMN + "WORKING"), (
        f"the row's text does not start at the queue's label column: {rendered!r}"
    )
    assert not rendered[:ROW_TEXT_COLUMN].strip(), (
        f"the row grew a mark of its own back: {rendered!r}"
    )


def _band(state: str, frame: int) -> "dict[int, str]":
    """The band at ``frame`` as ``{column: style}``, one entry per painted cell.

    Reading the rendering rather than re-deriving it: the test asks what the
    row looks like, and never recomputes the sweep with the arithmetic it is
    supposed to be checking.
    """
    content = activity_text(state, elapsed_s=None, width=80, shine_index=frame)
    return {span.start: str(span.style) for span in content.spans}


def test_the_shine_is_graded_not_two_valued() -> None:
    """Tier 1: the band has an interior — several distinct styles across its
    width — rather than the two states (on / off) it had before #3777.

    This is the defect the operator reported, stated as a property: a band
    whose cells are all-or-nothing has no edge to fall off, so it reads as a
    block blinking on and off rather than as a light travelling through the
    word. Asserting "more than two distinct styles" pins that there IS a ramp
    without pinning which colours the ramp passes through — the curve and its
    endpoints are free to change, the gradation is not.
    """
    band = _band("RESPONDING", frame=5)
    assert len(set(band.values())) > 2, (
        f"the band is still effectively two-valued: {sorted(set(band.values()))}"
    )


def test_the_shine_advances_one_cell_per_frame_carrying_its_shape() -> None:
    """Tier 1: consecutive frames are the same band one column further right.

    Checked as a SHIFT of the previous frame rather than against a table of
    expected positions, so the test states the property that matters (the
    light moves, and moves rigidly) without re-implementing the position
    arithmetic it is checking. Frames are chosen so the whole band is inside
    the word — at the two ends it is partly off the edge, which is the
    entering and leaving that :func:`_apply_shine` exists to produce and is
    covered by the clearance test below.
    """
    state = "WORKING"
    for frame in (4, 5):
        before, after = _band(state, frame), _band(state, frame + 1)
        assert after == {column + 1: style for column, style in before.items()}, (
            f"frame {frame} -> {frame + 1} was not a one-column shift: "
            f"{before!r} then {after!r}"
        )


def test_the_shine_degrades_to_an_attribute_without_colour() -> None:
    """Tier 1: with no colour available the band is still drawn, as an SGR
    attribute rather than as nothing.

    A gradient needs colour; where the terminal has none, the row must still
    show that a turn is running. Degrading to an attribute keeps that signal
    on a terminal a colour would have left blank — and it degrades to the
    form #3779 already shipped, so the fallback is a shape this row has been
    seen in rather than a third design nobody has looked at.
    """
    band = _band_without_colour = {
        span.start: str(span.style)
        for span in activity_text(
            "WORKING", elapsed_s=None, width=80, shine_index=3, colour=False
        ).spans
    }
    assert band, "no colour meant no band at all"
    assert set(band.values()) == {"reverse"}, (
        f"expected the attribute fallback, got {sorted(set(band.values()))}"
    )


def test_no_shine_band_while_shine_index_is_none() -> None:
    """Tier 1: a static row (no turn showing, or the caller opts out) paints
    no band at all — the parameter's absence is absence of the effect, not a
    band parked at index 0."""
    content = activity_text("WORKING", elapsed_s=None, width=80, shine_index=None)
    assert content.spans == []


def test_any_frame_number_is_a_valid_frame() -> None:
    """Tier 1: the cycle is wrapped inside ``activity_text``, so an arbitrarily
    large frame paints the same band as its position within the cycle.

    The widget's counter only ever increments — it does not wrap, because the
    cycle's length depends on ``len(state)`` and that changes on every
    ``specialise``. A counter carried across a state change therefore lands
    outside the previous cycle routinely, and if that painted nothing the row
    would go dark while a turn was visibly still running. Pinning the wrap
    here is pinning that it cannot.
    """
    state = "WORKING"
    # The band crosses the whole rendered row (#3777), so the pass is as long
    # as the row is — read from the rendering rather than recomputed, since
    # the row's width is padding-dependent and a formula here would be a
    # second implementation of the thing under test.
    row = str(activity_text(state, elapsed_s=None, width=80, shine_index=0))
    cycle = len(row) + 2 * (_SHINE_WIDTH // 2)
    assert _band(state, 999), "a large frame number painted no band at all"
    assert _band(state, 999) == _band(state, 999 % cycle)


def test_the_shine_crosses_the_whole_row_including_the_clock_and_hint() -> None:
    """Tier 1: the band is no longer confined to ``state``'s own span.

    #3779 kept it inside the state word so it could not wander onto the clock
    or the hint, which was right for a row that was three things sharing a
    line. #3777 removed the glyph and made the row read as one object, and the
    owner asked for the light to cross all of it. Pinned by sweeping a whole
    cycle and requiring that SOME frame paints past the state word — a band
    that silently kept the old confinement would still look plausible frame by
    frame, and only a sweep catches it.
    """
    state = "RESPONDING"
    body_end = ROW_TEXT_COLUMN + len(state)
    reached_beyond = False
    for frame in range(80):
        content = activity_text(
            state, elapsed_s=5.0, width=78, behind=12, shine_index=frame
        )
        assert content.spans, f"frame {frame} painted no band at all"
        if any(span.start >= body_end for span in content.spans):
            reached_beyond = True
        rendered = str(content)
        assert "LIVE +12" in rendered, f"frame {frame}: LIVE +N is missing: {rendered!r}"
    assert reached_beyond, (
        "the band never left the state word across a full sweep — it is still "
        "confined the way #3779 had it"
    )


@pytest.mark.asyncio
async def test_the_shine_advances_one_frame_per_tick_call() -> None:
    """Tier 2b: :meth:`ActivityRow.tick` called directly — the frame
    function for the STATEFUL widget, same discipline as the pure-function
    gates above, never a wait for the real timer to fire N times."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        row = app.query_one(ActivityRow)

        positions: "list[int]" = []
        for _ in range(4):
            row.tick()
            span = row.render().spans[0]
            positions.append(span.start)

        assert positions == sorted(positions), (
            f"the band did not advance monotonically over 4 direct ticks: {positions!r}"
        )
        assert len(set(positions)) > 1, (
            f"4 ticks produced no movement at all: {positions!r}"
        )


@pytest.mark.asyncio
async def test_tick_is_a_no_op_once_the_turn_has_ended() -> None:
    """Tier 2b: the safety property that actually matters — whether or not
    the underlying ``Timer`` object is paused (private, unobservable from
    here per CLAUDE.md's testing policy), a tick landing after ``end()`` for
    any reason must never repaint a row that claims nothing is happening."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        row = app.query_one(ActivityRow)
        assert row.display is True

        await transport.push_event(Event(type="turn_completed", data={"chain_id": "c1"}))
        await _settle(pilot)
        assert row.display is False
        before = str(row.render())

        row.tick()
        row.tick()
        row.tick()

        assert row.display is False
        assert str(row.render()) == before, (
            "a tick after end() changed the hidden row's content"
        )
