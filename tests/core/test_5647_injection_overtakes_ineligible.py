"""Tier 2: #5647 — mid-turn injection reaches the operator's message even when
machine-originated work arrived first.

#3792 built mid-turn injection so a human can steer a tool loop that is already
running. It also ruled that an ineligible queue head STOPS the peek, reasoning
that looking past it "would silently reorder arrival, and that reordering would
leave no trace anywhere".

Measured consequence (owner-reported, reyn-self WAL): a ``broker_drain`` hook
posts ``msg_kind=hook`` inbox items continuously, so the operator's prompt was
nearly always sitting behind one. The STOP rule therefore disabled the feature
precisely in the sessions that use it most — the prompt was never injected, and
sat visible in the TUI's sent-queue until the turn boundary.

#5647 (architect ruling A) lets injection look past ineligible items, and
answers both halves of #3792's objection rather than dismissing them:

- *reordering* — arrival order is NOT changed. Only which message the model
  sees first WITHIN a turn changes. The buffer is FIFO and ``consume_inbox``
  drains it before the queue, so turns still START in arrival order. Three
  tests below are about nothing but that.
- *no trace* — the mid-turn ``turn_started`` event now carries
  ``skipped_over``, enumerating kind and msg_id of everything looked past, in
  arrival order.

**#5747 update**: the ``broker_drain`` HOOK example above is now the
WRONG illustration of "ineligible" — #5747 added ``TurnOrigin.HOOK`` to
``MID_TURN_INJECTABLE`` itself, so a hook item sitting in front of the
operator's message is no longer looked past at all; it is now INJECTED
alongside it (``peek_mid_turn_injections`` collects every eligible item
in the scan, #5677's own "溜まっているものは基本inject すべき" ruling —
see that method's own docstring). The historical incident narrative
above is left as written (it is what #5647 was actually built to fix,
and remains true as history); the tests below now use ``TurnOrigin.CRON``
as their running example of a kind that stays genuinely ineligible
(cron is not a lifecycle hook — see ``TurnOrigin.CRON``'s own docstring
for why), so the "look past an ineligible item" mechanism this file
exists to test still has a real subject to exercise it against.

Real ``Session`` / ``StateLog`` / ``SnapshotJournal`` / real
``asyncio.Queue`` throughout — no mocks, no fakes, no hand-rolled arbiter. The
router-loop side of the seam (splicing peek's payload onto the wire tail) is
unchanged by #5647 and keeps its own witnesses in
``tests/llm/test_3792_pr2_router_loop_injection.py``; what changed, and what is
tested here, is which item peek returns.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

AGENT = "injection-overtake-agent"

_CRON = {"name": "broker_drain"}


def _make_session(wal: Path, snapshot_path: Path) -> "tuple[Session, StateLog]":
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _mid_turn_started(events) -> "list[dict]":
    """The ``turn_started`` events a mid-turn commit emitted, in order."""
    return [e.data for e in events if e.type == "turn_started"]


# ---------------------------------------------------------------------------
# accept — the operator's message is reached, and the trace is left
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_reaches_the_operator_message_behind_two_hooks(tmp_path):
    """Tier 2: #5647's acceptance criterion, and the owner's exact shape —
    inbox is [hook, hook, user].

    Before #5647 the peek returned ``None`` here and the operator's prompt
    waited for the turn boundary, which is what the owner saw. Now it is found,
    and the ``turn_started`` event enumerates what was looked past so the
    overtaking is on the record rather than silent.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    events = collect_events(session)

    await session._put_inbox(TurnOrigin.CRON, dict(_CRON))
    await session._put_inbox(TurnOrigin.CRON, dict(_CRON))
    await session.submit_user_text("stop and look at this")

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked, (
        "the operator's message must be reachable past the two hook items — "
        "returning [] here IS the reported defect"
    )
    (only,) = peeked
    assert only["payload"]["text"] == "stop and look at this"

    await session._commit_mid_turn_injection(only["msg_id"])
    await settle(session)

    starts = _mid_turn_started(events)
    (start,) = starts  # exactly one mid-turn commit happened
    skipped = start.get("skipped_over")
    assert skipped is not None, (
        "the overtaking must leave a trace — #3792's objection to skipping "
        "was that it would leave none, and this field is the answer to it"
    )
    assert [s["kind"] for s in skipped] == [TurnOrigin.CRON, TurnOrigin.CRON], skipped

    await state_log.aclose()


@pytest.mark.asyncio
async def test_the_overtaken_items_are_still_consumed_first_and_in_order(tmp_path):
    """Tier 2: the invariant #3792 was protecting, kept. Injection changes
    which message the MODEL sees first inside a turn; it does not change the
    order turns are started in.

    Strip-falsifier: draining the queue before the peek buffer in
    ``consume_inbox`` turns this red — the user item would come back first,
    having already been injected, and the hooks would be reordered behind it.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(TurnOrigin.CRON, {"name": "first"})
    await session._put_inbox(TurnOrigin.CRON, {"name": "second"})
    await session.submit_user_text("steer")

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = peeked
    await session._commit_mid_turn_injection(only["msg_id"])

    first = await session._inbox_arbiter.consume_inbox()
    second = await session._inbox_arbiter.consume_inbox()
    assert first is not None and second is not None
    assert [first[0], second[0]] == [TurnOrigin.CRON, TurnOrigin.CRON]
    assert [first[1]["name"], second[1]["name"]] == ["first", "second"], (
        "the two overtaken hooks must keep THEIR arrival order relative to "
        "each other, not just relative to the injected message"
    )

    await state_log.aclose()


@pytest.mark.asyncio
async def test_items_that_arrived_after_the_operator_keep_their_place_too(tmp_path):
    """Tier 2: [hook A, user, hook B]. The injection takes the user message
    from the middle; A and B are consumed at the boundary in arrival order.

    B is the case a buffer-only implementation gets wrong: it never entered
    the peek buffer at all (the scan stopped at the user message), so the
    ordering has to hold ACROSS the buffer/queue boundary, not just within
    the buffer.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(TurnOrigin.CRON, {"name": "A"})
    await session.submit_user_text("steer")
    await session._put_inbox(TurnOrigin.CRON, {"name": "B"})

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = peeked
    assert only["payload"]["text"] == "steer"
    await session._commit_mid_turn_injection(only["msg_id"])

    first = await session._inbox_arbiter.consume_inbox()
    second = await session._inbox_arbiter.consume_inbox()
    assert first is not None and second is not None
    assert [first[1]["name"], second[1]["name"]] == ["A", "B"]

    await state_log.aclose()


# ---------------------------------------------------------------------------
# deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_injection_when_no_operator_message_is_queued(tmp_path):
    """Tier 2: deny — hooks alone are never injected. Eligibility is unchanged
    by #5647: looking PAST an ineligible item is not the same as making it
    eligible, and this is the test that keeps those two apart.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    events = collect_events(session)

    await session._put_inbox(TurnOrigin.CRON, dict(_CRON))
    await session._put_inbox(TurnOrigin.CRON, dict(_CRON))

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked == []
    await settle(session)
    assert _mid_turn_started(events) == [], "no injection means no turn_started"

    # And nothing was lost: both hooks still drain, in arrival order.
    first = await session._inbox_arbiter.consume_inbox()
    second = await session._inbox_arbiter.consume_inbox()
    assert first is not None and second is not None
    assert [first[0], second[0]] == [TurnOrigin.CRON, TurnOrigin.CRON]

    await state_log.aclose()


@pytest.mark.asyncio
async def test_an_operator_message_at_the_head_reports_an_empty_skipped_list(tmp_path):
    """Tier 2: deny sibling for the trace. Nothing was overtaken, so
    ``skipped_over`` is an empty list — present, not absent.

    The key is always there on purpose: an ABSENT field cannot be told apart
    from "emitted by a build that could not overtake anything", which is the
    same conflation this codebase's #5009 pass exists to close. A reader must
    be able to distinguish "overtook nothing" from "does not report".
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    events = collect_events(session)

    await session.submit_user_text("nothing ahead of me")

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = peeked
    await session._commit_mid_turn_injection(only["msg_id"])
    await settle(session)

    starts = _mid_turn_started(events)
    (start,) = starts  # exactly one mid-turn commit happened
    assert "skipped_over" in start, (
        "the key must be present even when empty — absence is not the same "
        "claim as 'nothing was overtaken'"
    )
    assert start["skipped_over"] == []

    await state_log.aclose()


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_operator_message_is_passed_over_for_the_next_one(tmp_path):
    """Tier 2: a cancelled candidate is discarded and the scan continues to the
    next eligible one — the same rule ``consume_inbox``'s skip-at-consume
    already applies (#3300 P3). Overtaking must not resurrect a message the
    operator withdrew.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(TurnOrigin.CRON, dict(_CRON))
    cancelled_id = await session.submit_user_text("withdrawn")
    await session.submit_user_text("the real one")
    await session.cancel_queued(cancelled_id)

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = peeked
    assert only["payload"]["text"] == "the real one", (
        "the cancelled message must not be injected; the scan continues past "
        "it to the next eligible candidate"
    )

    await state_log.aclose()


# ---------------------------------------------------------------------------
# crash-recovery band — the buffer is volatile, the journal is the SSoT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overtaken_items_survive_a_wal_truncation_in_arrival_order(tmp_path):
    """Tier 2: CLAUDE.md's recovery-feature gate, on the state #5647 adds.

    The peek buffer is in-memory and holds MORE items than #3792's single slot
    did, so the question "what happens if the process dies mid-overtake" now
    has a bigger answer surface. It must be the same answer: the buffer is not
    the source of truth — the journal is. Peeking commits nothing, so a
    truncation below the reconstruction point must still leave every un-consumed
    item present, in arrival order.

    Falsify direction: were the peek to consume or reorder journal entries, the
    reconstructed inbox would come back short or shuffled.
    """
    wal, snap_path = tmp_path / "s.wal", tmp_path / "s.json"
    session, state_log = _make_session(wal, snap_path)

    await session._put_inbox(TurnOrigin.CRON, {"name": "A"})
    await session.submit_user_text("steer")
    await session._put_inbox(TurnOrigin.CRON, {"name": "B"})

    # Overtake, but never commit — the abnormal-exit shape.
    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked
    await settle(session)
    await state_log.aclose()

    reloaded = StateLog(wal)
    snap = AgentSnapshot.load(AGENT, snap_path)
    snap.apply_events(list(reloaded.iter_from(snap.applied_seq)))

    kinds = [entry.get("kind") for entry in snap.inbox]
    assert kinds == [TurnOrigin.CRON, TurnOrigin.CLIENT_INPUT, TurnOrigin.CRON], (
        f"a peek commits nothing, so every item must survive reconstruction, "
        f"in arrival order — the volatile buffer must not have reordered the "
        f"durable record: {kinds!r}"
    )

    await reloaded.aclose()


@pytest.mark.asyncio
async def test_the_ride_along_drain_does_not_read_past_the_peek_buffer(tmp_path):
    """Tier 2: a defect this PR introduced and then closed — kept as its own
    witness because nothing else in the suite covers the path.

    ``drain_to_wake`` collects ``wake=false`` ride-alongs with a NON-BLOCKING
    read after its first blocking one. That read used ``inbox.get_nowait()``
    directly. While the peek buffer held at most one item (#3792) that was
    correct, because ``consume_inbox`` had just emptied it. #5647 lets the
    buffer hold several — so the direct read would skip straight past items 2..N
    and return something that arrived LATER, which is precisely the reordering
    this design promises does not happen.

    Here the peek buffers two ride-alongs before the operator's message; the
    drain must return both, in arrival order, rather than jumping to the item
    still sitting in the queue behind them.

    Strip-falsifier: restore ``self._inbox.get_nowait()`` in ``drain_to_wake``'s
    inner loop and this goes red — the second ride-along is skipped and the
    trigger arrives without it.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(TurnOrigin.CRON, {"name": "ride-1", "wake": False})
    await session._put_inbox(TurnOrigin.CRON, {"name": "ride-2", "wake": False})
    await session.submit_user_text("the trigger")

    # The peek pulls both ride-alongs into the buffer on its way to the user
    # message, which is what makes the buffer hold more than one item.
    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = peeked
    await session._commit_mid_turn_injection(only["msg_id"])

    await session._put_inbox(TurnOrigin.CRON, {"name": "trigger", "wake": True})
    ride_alongs, trigger = await session._inbox_arbiter.drain_to_wake()

    assert [p["name"] for _k, p in ride_alongs] == ["ride-1", "ride-2"], (
        "both buffered ride-alongs must be drained, in arrival order — "
        "reading the queue directly skips the ones the peek is holding"
    )
    assert trigger is not None and trigger[1]["name"] == "trigger"

    await state_log.aclose()
