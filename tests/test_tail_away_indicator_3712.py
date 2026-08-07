"""#3712 — say how far behind the newest output the reader is, and how to return.

Scrolling back through a long reply stops the conversation following the tail.
Output keeps arriving and nothing says so: the pane looks like the end of the
conversation because, from where the reader is, it is.

The indicator rides in the live-turn row's spare space (#3693) rather than
claiming a row of its own — on the surface #3680 exists to protect, a permanent
row for an occasional state is expensive.

Two things this must not do, both of which the gates below pin:

- **name a key that does not work.** The conversation pane has `end` and `G`,
  but they fire only while IT holds focus — never from the composer, which is
  where a reader scrolling back actually is. So the hint names an app-level
  binding, and a gate presses that key rather than asserting the text.
- **count something the reader did not experience.** The unit is ENTRIES landed
  since they left, not rows: a row count would climb while a single streamed
  reply grew, reporting movement that never happened.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import (
    LATEST_HINT,
    ActivityRow,
    activity_text,
)
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


def _live_count(row) -> "int | None":
    """The number the row is reporting, or ``None`` if it reports nothing."""
    match = re.search(r"LIVE \+(\d+)", str(row.render()))
    return int(match.group(1)) if match else None


async def _settle_until_stable(pilot, read, *, arrived=lambda: True) -> "int":
    """Pump until ``read()`` has returned the SAME value on two consecutive
    checks taken while ``arrived()`` holds. Returns the stabilised value.

    Two consecutive equal reads is CONVERGENCE, not COMPLETION on its own —
    if the thing being measured updates in more than one pass, two samples
    could land on the same plateau mid-update while more input is still
    arriving. ``arrived()`` (default: always true, i.e. plain stabilisation)
    lets a caller name the OTHER condition that must hold before a plateau
    counts, and resets the stability count whenever it stops holding — see
    :func:`_fill_and_leave_the_tail`'s own use for why both are needed
    together in that case (#3770).

    Unbounded per the owner's testing policy
    (docs/deep-dives/contributing/testing.md, ## Time): no test carries a
    time budget, marker, or in-body pause-count cap — a slower environment
    only makes this slower, never wrongly satisfied. A bounded loop that
    silently returns the LAST (possibly still-unstable) value on exhaustion
    is exactly the #3770 defect shape recurring one level up: "N pumps is
    enough" was the bet that failed; a bigger N is a bigger bet, not a
    fix. CI's own timeout is the blast-radius kill-switch, not a per-test
    contract."""
    stable_since: "object | None" = None
    while True:
        await pilot.pause()
        await asyncio.sleep(0.01)
        if not arrived():
            stable_since = None
            continue
        current = read()
        if stable_since is not None and current == stable_since:
            return current
        stable_since = current


async def _settle_until(pilot, until) -> None:
    """Pump until ``until()`` holds.

    The measurement this file observes is DEFERRED to after a refresh (it
    needs a laid-out view, see ``_refresh_tail_indicator``). Unbounded per
    the owner's testing policy (docs/deep-dives/contributing/testing.md,
    ## Time) — no fixed pause-count budget: a slower environment only
    makes this slower, never wrongly satisfied. A prior version of this
    helper bounded the loop at a fixed pump count and silently returned on
    exhaustion instead of continuing to wait, which is the #3770 defect
    shape ("N is enough") recurring in this same file's own polling
    helper — flagged in review rather than caught by CI going red, since a
    generous-enough N mostly just makes the false success rarer, not
    absent."""
    while not until():
        await pilot.pause()
        await asyncio.sleep(0.01)


async def _fill_and_leave_the_tail(transport, pilot, app, *, lines: int = 40):
    """Put enough behind the reader that scrolling up genuinely leaves the tail.

    #3770: waits for ``max_scroll_y`` itself to STOP CHANGING before
    scrolling, deliberately, not as a stabilisation nicety. Measured
    (textual-flowview#12): a scroll-away delivered while ``max_scroll_y`` is
    still small/zero (content still arriving) is swallowed upstream —
    flowview's own ``_follow_bottom`` latch re-reads ``new_value >=
    max_scroll_y`` at the moment the scroll lands, and while the viewport
    has little or nothing to scroll away FROM yet that condition holds
    regardless of the reader's intent, so the "leave the tail" this fixture
    exists to set up silently does not happen. A fixed pause COUNT before
    the earlier version's ``_settle(pilot, 8)`` was a bet that 8 pumps is
    enough for all ``lines`` entries to land and lay out — true on this
    machine, not guaranteed elsewhere, which is what #3770 traced.

    The wait joins TWO conditions (:func:`_settle_until_stable`):
    ``len(flow.entries) >= lines`` (all pushed messages actually landed —
    without this, ``max_scroll_y`` could stabilise on a stale value mid-way
    through delivery, since flowview's layout can run in more than one
    pass) AND ``max_scroll_y`` reading the SAME value on two consecutive
    checks taken after entries arrive (layout has actually caught up — an
    earlier version of this fix used entries alone, which is a real but
    DIFFERENT event from flowview's layout pass finishing and growing
    ``max_scroll_y``; they only happened to land on the same tick in this
    fixture's own push-then-settle pattern, which is not a guarantee). One
    condition alone leaves the other's hole open; together they remove the
    bet rather than replacing it with a smaller one. This does not paper
    over flowview's swallowed-intent question, which stays open upstream.
    """
    await transport.push_event(
        Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
    )
    for i in range(lines):
        await transport.push_display(OutboxMessage(kind="agent", text=f"line {i}", meta={}))
    flow = app.query_one(FlowView)
    stable_max = await _settle_until_stable(
        pilot, lambda: flow.max_scroll_y, arrived=lambda: len(flow.entries) >= lines
    )
    assert stable_max > 0, (
        "test setup: max_scroll_y stabilised at 0 — nothing overflowed the "
        "viewport, so there is no tail to leave"
    )
    flow.scroll_to(y=0, animate=False)
    # #3770: waits for the scroll's OWN deferred _refresh_tail_indicator to
    # actually run (app._following_tail flips False) before this fixture
    # returns — callers push further arrivals right after and need
    # _note_entry_landed to already be counting them, which only happens
    # once the deferred measurement has settled the departure. The wait
    # predicate reads private state (``_following_tail``) because no public
    # surface distinguishes "measurement not yet run" from "run, and the
    # reader happens to be 0 entries behind" — the row's own text renders
    # the same either way (see #3770's own tracking comment for that gap).
    # CLAUDE.md's private-state ban is about ASSERTING on it as a verdict;
    # this only decides WHEN to look, and the assert two lines down stays on
    # the public ``scroll_y``/``max_scroll_y`` surface.
    await _settle_until(pilot, lambda: not app._following_tail)
    # #3720 diagnostic: CI and this machine disagree on the same SHA, and the
    # rendered row is wider there — both point at FlowView's real size, which
    # #3724's compact_caps can change. Printed so the two can be compared
    # side by side rather than guessed at.
    print(
        f"[3720] screen={app.size} flow={flow.size} virtual={flow.virtual_size} "
        f"scroll_y={flow.scroll_y} target={flow.scroll_target_y} "
        f"max={flow.max_scroll_y} entries={len(flow.entries)} "
        f"activity={app.query_one(ActivityRow).size}",
        flush=True,
    )
    assert flow.scroll_y < flow.max_scroll_y, (
        "the conversation did not actually leave the tail, so nothing below "
        "this is being exercised"
    )
    return flow


def test_the_indicator_takes_the_slot_ahead_of_the_cancel_hint() -> None:
    """Tier 1: when both want the right-hand slot, the LIVE count wins.

    Someone reading back cannot see the new output arriving; the cancel key
    works whether or not it is printed. So the more urgent one is shown.
    """
    with_behind = str(activity_text("RESPONDING", elapsed_s=5.0, width=78, behind=12))
    without = str(activity_text("RESPONDING", elapsed_s=5.0, width=78))

    assert "LIVE +12" in with_behind
    assert "Ctrl+C cancel" not in with_behind
    assert "Ctrl+C cancel" in without


def test_a_narrow_row_drops_the_hint_rather_than_cutting_it() -> None:
    """Tier 1: never a clipped key.

    `Ctrl+End latest` cut short still reads as a complete instruction and is a
    different one. The state survives; the suffix is printed whole or not at
    all.
    """
    narrow = str(activity_text("RESPONDING", elapsed_s=5.0, width=30, behind=12))

    assert "RESPONDING" in narrow
    assert LATEST_HINT not in narrow
    assert "LIVE" not in narrow


@pytest.mark.asyncio
async def test_nothing_is_shown_while_the_reader_is_on_the_newest_output() -> None:
    """Tier 2b: following the tail is the ordinary case and says nothing.

    An indicator that were always up would stop meaning "you are missing
    something".
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            Event(type="turn_started", data={"kind": "user", "chain_id": "c1", "seq": 1})
        )
        for i in range(10):
            await transport.push_display(
                OutboxMessage(kind="agent", text=f"line {i}", meta={})
            )
        # #3770: the negative assertion below can't be proven by waiting
        # (#3327's line) — but a POSITIVE witness that the 10 pushed
        # entries actually landed is still required first. Without it, a
        # too-short settle would pass "LIVE not shown" vacuously (because
        # nothing has rendered yet, not because arrivals-while-away is
        # correctly not counted) — the same emptiness class fixed elsewhere
        # in this file today, just on the negative side.
        await _settle_until(pilot, lambda: len(app.query_one(FlowView).entries) >= 10)

        assert "LIVE" not in str(app.query_one(ActivityRow).render())


@pytest.mark.asyncio
async def test_output_arriving_while_away_is_counted() -> None:
    """Tier 2b: entries landing after the reader left are reported.

    The count starts when they leave, not from the beginning of the
    conversation — what matters is what they have not seen.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _fill_and_leave_the_tail(transport, pilot, app)

        for i in range(3):
            await transport.push_display(
                OutboxMessage(kind="agent", text=f"new {i}", meta={})
            )
        row = app.query_one(ActivityRow)
        await _settle_until(pilot, lambda: "LIVE" in str(row.render()))

        rendered = str(row.render())
        assert "LIVE +3" in rendered, f"the arrivals were not reported: {rendered!r}"


@pytest.mark.asyncio
async def test_the_named_key_actually_returns_to_the_newest_output() -> None:
    """Tier 2b: the printed key is pressed, not assumed.

    The conversation pane's own `end`/`G` fire only while it holds focus, so a
    hint naming them would be wrong from the composer — which is where the
    reader is. Pressing the key the row prints is the only way this gate can
    tell a working affordance from a plausible-looking string.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _fill_and_leave_the_tail(transport, pilot, app)
        await transport.push_display(OutboxMessage(kind="agent", text="new", meta={}))
        row = app.query_one(ActivityRow)
        await _settle_until(pilot, lambda: "LIVE" in str(row.render()))
        assert "LIVE" in str(row.render())

        await pilot.press("ctrl+end")
        # #3770: no deferred wait needed here — action_jump_to_latest sets
        # scroll_y, _away_arrivals, _following_tail, and the row's own
        # ``set_behind(None)`` all SYNCHRONOUSLY inside the key handler (its
        # own docstring: "cleared HERE rather than through the deferred
        # measurement"), so once the keypress message has been dispatched
        # both assertions below are already final. A single pause is for
        # dispatch, not for a race.
        await pilot.pause()

        assert flow.scroll_y >= flow.max_scroll_y, "the key did not return to the tail"
        assert "LIVE" not in str(app.query_one(ActivityRow).render()), (
            "the indicator survived the return it was pointing at"
        )


@pytest.mark.asyncio
async def test_a_growing_reply_is_not_counted_as_arrivals() -> None:
    """Tier 2b: the unit is entries, and a streamed reply proves it.

    A reply arriving in pieces makes ONE entry taller. The reader missed one
    thing, not thirty — and they can see it is the same thing, because it is
    the reply they scrolled away from. Counting rows would report movement
    that never happened, climbing while nothing new arrived, which is the
    kind of number that is worse than none.

    This is the gate that a row-based implementation cannot pass: the middle
    assertion goes red the moment the count follows height instead of
    arrivals. Saying "entries, not rows" in a docstring is not the same as
    having something fail when it stops being true.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        flow = await _fill_and_leave_the_tail(transport, pilot, app)
        # #3770: no extra settle needed here — _fill_and_leave_the_tail's own
        # wait (above) already blocks until its deferred measurement has run,
        # so the state it returns is already what this baseline wants.
        row = app.query_one(ActivityRow)
        # Whatever the count is at this instant is the baseline. Asserting it
        # is zero would only be testing how the fixture happened to drain.
        baseline = str(row.render())

        # The reply the reader is looking at grows, in place, by a lot.
        before_entries = len(flow.entries)
        for i in range(30):
            await transport.push_event(
                Event(type="agent_delta", data={"chain_id": "c1", "text": f"chunk {i} "})
            )
        # #3770: waits for the entry COUNT to stop changing — a weak
        # condition ("delta processing has quieted down"), not "count ==
        # before_entries + 1", which would make the very next assert
        # redundant with the wait itself. The count folding to exactly one
        # new entry is what the assert below actually checks.
        await _settle_until_stable(pilot, lambda: len(flow.entries))

        landed = len(flow.entries) - before_entries
        assert landed == 1, (
            f"thirty deltas produced {landed} entries — they are supposed to "
            "fold into one reply, so this is not exercising the case"
        )

        reported = _live_count(row)
        assert reported == landed, (
            f"the row reported {reported} arrivals for {landed} entry — "
            "a reply growing in place is one thing arriving, not thirty. A "
            "count that follows rows fails here with a number near 30"
        )
