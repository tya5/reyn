"""#3693 — the live-turn line above the composer, and NEXT on the queue.

The RUNNING gutter is the right expression of a live turn inside the
conversation, and it scrolls away. This region answers "is the turn still
alive, and what is it doing" next to the composer, for the duration of the
turn only.

Every gate here is about NOT FABRICATING. The row may say only what the client
observed: a turn is running (``turn_active``), content is arriving
(``agent_delta``), a named tool is in flight (a labelled frame). A client that
attached mid-turn knows the first and none of the rest, and must not print an
elapsed time measured from when it happened to connect.

``NEXT`` is asserted as UNCONDITIONAL on a non-empty queue. An earlier version
of the proposal put it on the queue only while a turn was running; the owner
ruled that wrong — the queue holds undispatched inbox items, whether a turn is
in flight is a separate fact, and there is no dependency between them. Two
windows make that concrete and both are covered below: a message typed before
the session attached, and one sitting between a turn settling and the next
dispatch.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.content import Span

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import ActivityRow, activity_text
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
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


def _queued(text: str, *, msg_id: str, chain_id: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": {}},
    )


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


# ── NEXT: a property of the queue, not of the turn ───────────────────────────


@pytest.mark.asyncio
async def test_the_head_of_the_queue_is_labelled_next_with_no_turn_running() -> None:
    """Tier 2b: NEXT appears on a queued item while nothing is running.

    The window between one turn settling and the next dispatch. The item is
    still the next thing to be sent, so the label describes it correctly — and
    the version of this feature that hid the label here was the one the owner
    rejected.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_queued("review this", msg_id="m1", chain_id="c1", seq=1))
        await _settle(pilot)

        assert app.query_one(ActivityRow).display is False, "no turn is running here"
        rows = app.query_one(SentQueue).rendered_texts()
        assert rows and "NEXT" in rows[0], f"the head of the queue is unlabelled: {rows}"


@pytest.mark.asyncio
async def test_only_the_head_is_labelled_and_the_order_is_unchanged() -> None:
    """Tier 2b: NEXT marks one row, and the queue is otherwise as it was.

    #3300's contract — the ⧗ rows, their order, their individual selection and
    cancel — is not what this feature is changing.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for i in (1, 2, 3):
            await transport.push_event(
                _queued(f"message {i}", msg_id=f"m{i}", chain_id=f"c{i}", seq=i)
            )
        await _settle(pilot)

        rows = app.query_one(SentQueue).rendered_texts()
        labelled = [r for r in rows if "NEXT" in r]
        assert labelled == rows[:1], (
            f"NEXT marks something other than exactly the head: {rows}"
        )
        assert [r.split("⧗ ")[-1] for r in rows] == ["message 1", "message 2", "message 3"]


@pytest.mark.asyncio
async def test_promotion_still_happens_once_and_the_label_follows_the_new_head() -> None:
    """Tier 2b: dispatch promotes the head into the flow exactly once, and NEXT
    moves to whatever is now first.

    The label must not become a second way for an item to appear, and must not
    stick to an item that has left the queue.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_queued("first", msg_id="m1", chain_id="c1", seq=1))
        await transport.push_event(_queued("second", msg_id="m2", chain_id="c2", seq=2))
        await _settle(pilot)

        await transport.push_event(_started("c1", 3))
        await _settle(pilot)

        rows = app.query_one(SentQueue).rendered_texts()
        assert [r.split("⧗ ")[-1] for r in rows] == ["second"], (
            f"the dispatched item did not leave the queue: {rows}"
        )
        assert "NEXT" in rows[0], "the label did not follow the new head"

        from textual_flowview import FlowView

        promoted = [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]
        assert [str(e.item.text) for e in promoted] == ["first"], (
            "the dispatched item was promoted more or less than once"
        )


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


def test_now_is_gone_and_the_state_starts_at_column_zero() -> None:
    """Tier 1: the vacated 6-column ``NOW   `` slot is not left as a blank
    gutter — ``state`` now IS the row's first content."""
    rendered = str(activity_text("WORKING", elapsed_s=None, width=80))
    assert rendered.startswith("WORKING"), f"NOW's slot was not reclaimed: {rendered!r}"
    assert "NOW" not in rendered


def test_the_shine_is_a_two_character_band_inside_the_state_word() -> None:
    """Tier 1: the frame function directly, at explicit indices — not a
    real-time animation check (#3770-adjacent discipline: a frame function
    called n times with an expected shape, never a wall-clock wait for
    something to "look like it is animating").

    Six frames (one full pass across ``WORKING``, 7 characters) are checked
    by POSITION, not by re-deriving the sweep from the function under test."""
    state = "WORKING"
    expected_spans = [(0, 2), (1, 3), (2, 4), (3, 5), (4, 6), (5, 7)]
    for index, (start, end) in enumerate(expected_spans):
        content = activity_text(state, elapsed_s=None, width=80, shine_index=index)
        assert content.spans == [Span(start, end, style="reverse")], (
            f"frame {index}: expected the band at [{start}:{end}], got {content.spans!r}"
        )
        assert str(content).startswith(state), (
            f"frame {index}: the shine changed the STATE TEXT, not just its style: "
            f"{str(content)!r}"
        )


def test_no_shine_band_while_shine_index_is_none() -> None:
    """Tier 1: a static row (no turn showing, or the caller opts out) paints
    no band at all — the parameter's absence is absence of the effect, not a
    band parked at index 0."""
    content = activity_text("WORKING", elapsed_s=None, width=80, shine_index=None)
    assert content.spans == []


def test_a_shine_index_past_the_word_clamps_inside_it() -> None:
    """Tier 1: ``activity_text`` is defensive against an out-of-range index
    (the looping arithmetic lives in the WIDGET, per
    :meth:`ActivityRow.tick`'s ``% len(state)`` — this pins the pure
    function's own behaviour if that ever drifts or is bypassed)."""
    content = activity_text("WORKING", elapsed_s=None, width=80, shine_index=999)
    assert content.spans, "an out-of-range index painted no band at all"
    start, end, _style = content.spans[0]
    assert 0 <= start < len("WORKING")
    assert end <= len("WORKING")


def test_the_shine_never_touches_the_elapsed_clock_or_the_live_count() -> None:
    """Tier 1: the band is confined to ``state``'s own span — the elapsed
    clock and the right-aligned ``LIVE +N`` hint are DIFFERENT information
    and must never be drawn inside the travelling highlight, at any frame
    position across a full sweep."""
    state = "RESPONDING"
    for index in range(len(state)):
        content = activity_text(
            state, elapsed_s=5.0, width=78, behind=12, shine_index=index
        )
        start, end, _style = content.spans[0]
        assert end <= len(state), (
            f"frame {index}: the band reached past the state word into the "
            f"clock/hint: {content.spans!r}"
        )
        rendered = str(content)
        assert "LIVE +12" in rendered, f"frame {index}: LIVE +N is missing: {rendered!r}"


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
