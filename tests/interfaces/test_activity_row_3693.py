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

from reyn.interfaces import palette
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import (
    _CANCEL_HINT,
    LATEST_HINT,
    ActivityRow,
    _shine_ramp,
    activity_text,
)
from reyn.interfaces.inline.textual_chat.sent_queue import ROW_TEXT_COLUMN
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

    A client that joined mid-turn has no start instant. "0s" would read as a
    measurement — a turn that just began — and printing nothing reads as what
    it is.
    """
    with_clock = str(activity_text("WORKING", elapsed_s=78.0, width=80))
    without = str(activity_text("WORKING", elapsed_s=None, width=80))

    assert "01:18" in with_clock
    # The head is the segment before the first separator — the count that
    # follows it legitimately carries a digit, so the claim has to be scoped to
    # where a duration would appear rather than to the whole line.
    head = without.split("·")[0]
    assert not any(ch.isdigit() for ch in head), (
        f"a duration appeared where none was known: {without!r}"
    )


def test_the_clock_switches_format_at_a_minute() -> None:
    """Tier 1: bare seconds below a minute, ``MM:SS`` at and above it.

    Each is the readable one in its own range: ``00:12`` makes a twelve-second
    turn look like a stopwatch reading, and ``1247s`` is a number nobody reads
    at a glance. The boundary is pinned on both sides because "switches
    somewhere" is not the claim — a threshold that drifted to five minutes
    would leave four minutes of unreadable seconds and still pass a test that
    only checked the two extremes.
    """
    def clock(elapsed: float) -> str:
        return activity_text("RESPONDING", elapsed_s=elapsed, width=70).plain.split(" · ")[0]

    assert clock(12).endswith("12s")
    assert clock(59).endswith("59s")
    assert clock(60).endswith("01:00")
    assert clock(1247).endswith("20:47")


def test_the_cancel_affordance_yields_before_the_state_does() -> None:
    """Tier 1: on a narrow row the state survives and the hint is what goes.

    Both cannot fit at every width. The state is the thing the row exists to
    show; the hint names a key that works whether or not it is printed.
    """
    wide = str(activity_text("RESPONDING", elapsed_s=5.0, width=80, entries=3))
    narrow = str(activity_text("RESPONDING", elapsed_s=5.0, width=40, entries=3))

    assert _CANCEL_HINT in wide
    assert "RESPONDING" in narrow
    assert len(narrow) <= len(wide)
    # What goes first is the count, not the way out: a row that gave up "how to
    # stop this" to keep a number would have its priorities backwards.
    assert _CANCEL_HINT in narrow, (
        f"the abort hint was dropped before the count: {narrow!r}"
    )
    assert "3 entries" not in narrow


def test_a_row_too_narrow_for_the_state_keeps_the_state_and_overflows() -> None:
    """Tier 1: the head is never dropped, even when it alone will not fit.

    Everything optional goes first, and then the row overflows rather than
    truncating what is happening. A clipped state word is a different word —
    ``TOOL search_actions`` cut to ``TOOL sea`` names a tool that does not
    exist — and a row that has given up saying what is happening has nothing
    left to be. Pinned because the alternative (stop dropping once it fits, and
    clip the rest) is what a later reader would reach for on seeing the loop
    run to empty.
    """
    narrow = str(activity_text("RESPONDING", elapsed_s=5.0, width=4, entries=3))

    assert "RESPONDING" in narrow, f"the state was clipped away: {narrow!r}"
    assert "entries" not in narrow and _CANCEL_HINT not in narrow, (
        f"an optional segment survived a width nothing fits in: {narrow!r}"
    )
    # "RESPONDING" in narrow already says the row is wider than the four
    # columns it was given: overflowing is the intended outcome, and asserting
    # it a second time by size would pin the shape rather than the behaviour.


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


def _cells(state: str, phase: float, *, dark: bool = True) -> "dict[int, str]":
    """The rendered row as ``{column: style}``, one entry per painted cell.

    Reading the rendering rather than re-deriving it: the test asks what the
    row looks like and never recomputes the sweep with the arithmetic it is
    supposed to be checking.
    """
    content = activity_text(
        state, elapsed_s=None, width=80, shine_phase=phase, dark=dark
    )
    return {span.start: str(span.style) for span in content.spans}


def test_every_character_is_painted_so_the_band_has_no_free_edge() -> None:
    """Tier 1: no cell is left at the terminal's own foreground.

    This is the defect the owner reported: with only the band painted, its
    outermost cells were a dark colour sitting directly against an undimmed
    ground, and the row read as THREE things moving instead of one. A uniform
    ground under a single bright band is what reads as one light, and "uniform"
    means every cell — so the property to hold is coverage, stated as coverage
    rather than as a colour comparison that a future palette change would have
    to be taught about.
    """
    row = activity_text("RESPONDING", elapsed_s=12, width=60, shine_phase=0.35)
    painted = {span.start for span in row.spans}
    assert painted == set(range(len(row.plain))), (
        "cells left unpainted (they keep the terminal foreground and read as a "
        f"step at the band's edge): {sorted(set(range(len(row.plain))) - painted)}"
    )


def test_the_ramp_ends_at_the_ground_so_the_band_has_no_step() -> None:
    """Tier 1: the band's faintest cell IS the ground the rest of the row wears.

    Coverage alone is not enough — every cell could be painted and the band's
    outermost cell still be a colour nothing else on the row is wearing, which
    is the same visible edge in a different disguise. Ending the ramp exactly
    at the ground is what removes the edge, and it is the invariant the earlier
    shape broke: it stopped the ramp at a dark value and left the ground
    undrawn, so both ends of the band showed as their own moving features.
    """
    ramp = _shine_ramp(3, palette.SHINE_GROUND_DARK, palette.SHINE_PEAK_DARK)
    assert ramp[-1] == palette.SHINE_GROUND_DARK, (
        f"the band's outer cell is not the ground: {ramp[-1]} vs "
        f"{palette.SHINE_GROUND_DARK}"
    )
    assert ramp[0] == palette.SHINE_PEAK_DARK


def test_the_shine_is_graded_not_two_valued() -> None:
    """Tier 1: the band has an interior — several distinct styles across its
    width — rather than the two states (on / off) it had before #3777.

    A band whose cells are all-or-nothing has no edge to fall off, so it reads
    as a block blinking rather than as a light travelling. The property is that
    an INTERMEDIATE exists — at least one cell that is neither fully lit nor
    the ground — which says the ramp is there without saying how many steps it
    takes or which colours it passes through.
    """
    styles = set(_cells("RESPONDING", phase=0.5).values())
    extremes = {palette.SHINE_PEAK_DARK, palette.SHINE_GROUND_DARK}
    assert styles - extremes, (
        f"every cell is either fully lit or ground — the band is two-valued "
        f"and will read as a block blinking: {sorted(styles)}"
    )


def test_the_band_moves_with_the_phase() -> None:
    """Tier 1: a later phase puts the bright end further right.

    Pinned as "the peak moved right", not as a table of positions: the
    positions depend on the row's width and the band's fraction, and a test
    that restated them would be a second implementation of the thing it checks.
    """
    def peak_column(phase: float) -> int:
        cells = _cells("RESPONDING", phase=phase)
        counts: "dict[str, int]" = {}
        for style in cells.values():
            counts[style] = counts.get(style, 0) + 1
        ground = max(counts, key=lambda style: counts[style])
        lit = [column for column, style in cells.items() if style != ground]
        return sum(lit) // len(lit)

    assert peak_column(0.25) < peak_column(0.6), "the band did not advance with the phase"


def test_a_light_terminal_gets_a_different_pair_than_a_dark_one() -> None:
    """Tier 1: the ground/peak pair follows the terminal's background.

    One pair cannot serve both. The dark pair's peak is nearly white, so on a
    white terminal it is not a subtle band, it is an absent one — the row would
    silently stop showing that a turn is running for anyone on a light theme.
    """
    assert set(_cells("RESPONDING", 0.5, dark=True).values()) != set(
        _cells("RESPONDING", 0.5, dark=False).values()
    ), "the same colours were used on both terminal grounds"


def test_the_shine_degrades_to_an_attribute_without_colour() -> None:
    """Tier 1: with no colour available the band is still drawn, as an SGR
    attribute rather than as nothing.

    A gradient needs colour; where the terminal has none the row must still
    show that a turn is running. It degrades to the form #3779 shipped, so the
    fallback is a shape this row has been seen in rather than a third design.
    """
    band = {
        span.start: str(span.style)
        for span in activity_text(
            "WORKING", elapsed_s=None, width=80, shine_phase=0.3, colour=False
        ).spans
    }
    assert band, "no colour meant no band at all"
    assert set(band.values()) == {"reverse"}, (
        f"expected the attribute fallback, got {sorted(set(band.values()))}"
    )


def test_no_shine_while_the_phase_is_none() -> None:
    """Tier 1: a static row (no turn showing) paints no band at all — the
    parameter's absence is absence of the effect, not a band parked at zero."""
    content = activity_text("WORKING", elapsed_s=None, width=80, shine_phase=None)
    assert content.spans == []


def test_the_shine_crosses_the_whole_row_including_the_clock_and_hint() -> None:
    """Tier 1: the band is not confined to ``state``'s own span.

    #3779 kept it inside the state word so it could not wander onto the clock
    or the hint, which was right for a row that was three things sharing a
    line. #3777 removed the glyph and made the row read as one object, and the
    owner asked for the light to cross all of it.
    """
    state = "RESPONDING"
    body_end = ROW_TEXT_COLUMN + len(state)
    reached_beyond = False
    for step in range(20):
        cells = _cells(state, phase=step / 20)
        counts: "dict[str, int]" = {}
        for style in cells.values():
            counts[style] = counts.get(style, 0) + 1
        ground = max(counts, key=lambda style: counts[style])
        if any(column >= body_end for column, s in cells.items() if s != ground):
            reached_beyond = True
    assert reached_beyond, (
        "the band never left the state word across a full pass — it is still "
        "confined the way #3779 had it"
    )


def test_the_count_does_not_depend_on_where_the_reader_is() -> None:
    """Tier 1: ``entries`` reads the same whether or not the reader is away.

    The previous shape made these one parameter, so the number's baseline was
    the instant the reader scrolled away — an event the reader cannot see, and
    therefore a number whose meaning there was no occasion to learn. Splitting
    them is the fix; this pins that they are actually independent, rather than
    the same value read twice under two names.
    """
    following = activity_text("RESPONDING", elapsed_s=12, width=80, entries=3)
    away = activity_text("RESPONDING", elapsed_s=12, width=80, entries=3, away=True)
    assert "3 entries" in following.plain
    assert "3 entries" in away.plain


def test_the_return_hint_appears_only_while_the_reader_is_away() -> None:
    """Tier 1: ``away`` decides one thing — whether the return hint prints.

    A hint offering to take the reader back to output they are already looking
    at is an instruction with nothing to do, and the row has no room to spend
    on one.
    """
    following = activity_text("RESPONDING", elapsed_s=12, width=80, entries=3).plain
    away = activity_text(
        "RESPONDING", elapsed_s=12, width=80, entries=3, away=True
    ).plain
    assert LATEST_HINT not in following
    assert LATEST_HINT in away
    assert _CANCEL_HINT in following and _CANCEL_HINT in away


def test_a_turn_that_has_produced_nothing_says_zero() -> None:
    """Tier 1: the count starts at zero and is shown, not hidden.

    A count that appears only once it is non-zero teaches its own scale late:
    the reader meets it already at 4 and has to infer what it counts. Starting
    visible at 0 means the first thing it ever shows is its baseline.
    """
    assert "0 entries" in activity_text("WORKING", elapsed_s=1, width=80).plain


@pytest.mark.asyncio
async def test_the_shine_position_follows_the_clock_not_the_tick_count() -> None:
    """Tier 2b: the band's position is a function of TIME, not of how many
    times :meth:`ActivityRow.tick` happened to be called.

    Driven by an injected clock, so no wall-clock waiting is involved: ticking
    without advancing the clock must not move the band, and advancing it must.
    A frame counter passes the second half and fails the first — and a counter
    is what makes one pass take longer on a wider row, which is the property
    #3777 replaced it to fix.
    """
    now = [1000.0]
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: now[0])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_started("c1", 1))
        await _settle(pilot)
        row = app.query_one(ActivityRow)

        def lit() -> "list[int]":
            content = row.render()
            counts: "dict[str, int]" = {}
            for span in content.spans:
                counts[str(span.style)] = counts.get(str(span.style), 0) + 1
            ground = max(counts, key=lambda style: counts[style])
            return [span.start for span in content.spans if str(span.style) != ground]

        before = lit()
        for _ in range(3):
            row.tick()
        assert lit() == before, (
            "ticking without time passing moved the band — the position is "
            "still counting frames"
        )

        now[0] += 1.0
        row.tick()
        assert lit() != before, "advancing the clock did not move the band"


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
