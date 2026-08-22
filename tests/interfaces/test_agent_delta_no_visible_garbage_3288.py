"""Tier 2: #3288 ③b/③c — the "no visible-garbage window" property for the
"agent_delta" audit-event, witnessed on the ACTUAL ``TextualChatApp`` pump
(production code untouched — imported and driven read-only from this test,
per the #3299/P5 non-interference constraint on
``interfaces/inline/textual_chat/``).

★③c re-point (issue #3288 comment thread, ③c phase): ③b's original assertion
here was "an agent_delta draws NOTHING" — true only UNTIL ③c lands a
consumer. ③c's whole job is to make ``agent_delta`` coalesce into a flow
entry (:meth:`TextualChatApp._handle_agent_delta_event`), which legitimately
FALSIFIES that literal assertion. The PROPERTY worth keeping (per the ③c
brief: "migrate, do not delete") is narrower and still true post-③c: **a
delta never produces a junk row of its own** — N deltas for one reply
produce exactly ONE flow entry (never N), and that entry's content is the
progressively-coalesced text, not a generic/garbage rendering of the raw
event payload. This file re-points the assertion to that property instead
of dropping the file's witness.

★co-vet fix (PR #3312 review, ③b): an EARLIER version of this file asserted
only "FlowView entry count unchanged" against the delta push — which stays
GREEN even if ``push_event``/the transport is silently dead (verified:
neutering ``QueueTransport.push_event`` into a no-op left both tests
passing), because "delivered but not drawn" and "never delivered" are
indistinguishable from that assertion alone. The fix is an ARRIVAL WITNESS
(positive control) that exercises the EXACT SAME ``push_event`` path the
delta assertion depends on: push a real ``user_submitted`` + matching
``turn_started`` pair FIRST (the proven promote-to-FlowView sequence from
``tests/interfaces/test_3300_p2b_sentqueue_render.py::test_turn_started_promotes_matching_item_to_flow_entry``)
and assert the FlowView entry count DOES increase — proving this transport +
pump pair is alive and forwarding EVENT frames — THEN push the delta(s) and
assert against that KNOWN-alive baseline. (A bare ``user_submitted`` alone
cannot serve as this witness: #3300 P2b made it materialize into the
sent-queue only, never a FlowView entry by itself — the promoting
``turn_started`` companion is required, mirroring the cited test exactly.)

★self-diagnosing failure (③c brief): if
``test_agent_delta_draws_exactly_one_coalesced_entry`` below fails at its
ARRIVAL-WITNESS step (before any delta is even pushed), the failure is a
regression in the EVENT delivery path itself (``push_event`` / ``frames()`` /
``turn_started`` promotion), NOT a ③c coalesce regression — the same
``user_submitted``+``turn_started`` pair this file has used since ③b is
reused unmodified as the positive control, so a failure there points at the
delivery mechanism, not at the "agent_delta" consumer this file is actually
about.

Real instances only: a real ``TextualChatApp`` driven via Textual's
``run_test()`` harness (mirrors ``tests/interfaces/test_user_submitted_render_3300.py``'s
``QueueTransport`` idiom) and a real ``FlowView`` query — no mocks.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _STREAM_REPAINT_MIN_INTERVAL
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class _DrivenClock:
    """The app's own ``clock`` injection point, driven instead of slept through
    (the idiom ``tests/interfaces/test_stream_spinner_3530.py`` uses for the blink).

    ★Load-bearing since #3570: a streamed reply's entry is repainted at most
    once per ``_STREAM_REPAINT_MIN_INTERVAL`` on THIS clock, so "push two deltas
    and read the row" is only a statement about the render if the test says when
    the budget window has passed. Left on the real clock, the assertion below
    passes or fails according to how long ``pilot.pause()`` happens to take on
    the machine — measured 9/20 failures at ~25 ms per pause and 0/20 at ~35 ms,
    which is a coin-flip CI gate, not a gate (the #3473 flake class). The
    accumulated TEXT is never affected either way: it is appended
    unconditionally, and only the ``set_item`` is budgeted."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def past_the_repaint_budget(self) -> None:
        """Move beyond the #3570 repaint window, so the NEXT delta repaints."""
        self.advance(_STREAM_REPAINT_MIN_INTERVAL * 2)


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (mirrors ``tests/interfaces/test_user_submitted_render_3300.py`` /
    ``tests/interfaces/test_3300_p2b_sentqueue_render.py``'s helper of the same name) —
    lets a test push a frame and inspect ``TextualChatApp``'s retained
    conversation model afterward."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

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


def _user_submitted(*, msg_id: str, chain_id: str, text: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": {}},
    )


def _turn_started(*, chain_id: str, seq: int) -> Event:
    return Event(type="turn_started", data={"kind": "user", "chain_id": chain_id, "seq": seq})


async def _arrival_witness(transport: QueueTransport, pilot, app: TextualChatApp) -> int:
    """Push a real ``user_submitted`` + matching ``turn_started`` pair through
    ``push_event`` (the SAME EventFrame path the delta assertions below
    depend on) and assert it PROMOTES to a FlowView entry — the proven
    promote sequence from
    ``tests/interfaces/test_3300_p2b_sentqueue_render.py::test_turn_started_promotes_matching_item_to_flow_entry``.
    Returns the FlowView entry count AFTER the witness promotion, so callers
    assert subsequent pushes against a KNOWN-alive baseline instead of an
    unverified "before" count. A dead/neutered ``push_event`` makes this
    assertion fail here, RED, before any delta-silence claim is even made."""
    before = len(app.query_one(FlowView).entries)
    await transport.push_event(
        _user_submitted(msg_id="witness-1", chain_id="witness-chain", text="arrival witness", seq=1)
    )
    await pilot.pause()
    await transport.push_event(_turn_started(chain_id="witness-chain", seq=2))
    await pilot.pause()
    after = len(app.query_one(FlowView).entries)
    assert after == before + 1, (
        "arrival witness failed — a user_submitted+turn_started pair did not "
        f"promote to a FlowView entry ({before} -> {after}); push_event/the "
        "transport+pump pair is not actually alive, so a delta-silence "
        "assertion elsewhere in this file would be meaningless"
    )
    return after


@pytest.mark.asyncio
async def test_agent_delta_draws_exactly_one_coalesced_entry() -> None:
    """Tier 2: ③c re-point of ③b's "draws nothing" — N ``agent_delta``
    audit-events for ONE reply must never produce more than ONE FlowView
    entry, and that entry's content must be the progressively-coalesced
    text — never a junk/generic row per delta (the property ③b's original
    assertion was guarding, restated for a world where ③c's consumer
    exists).

    ★vacuity fix (lead-coder correction on this PR): asserting ONLY at/after
    the terminal completion is itself vacuous — the completion frame alone
    creates one full-text entry regardless of whether any delta was ever
    delivered, so "1 entry, full text" would pass even with delta delivery
    completely dead. This test instead asserts at THREE cross-sections:

    1. after the arrival witness (baseline, proven alive) but BEFORE any
       delta — establishes the pre-delta count;
    2. after the FIRST delta only (mid-coalesce, no completion yet) — count
       must be baseline+1 and the entry's content must be that first
       delta's PARTIAL text (proves the delta itself is what created the
       entry — not the later completion);
    3. after a SECOND delta (still no completion) — count stays at
       baseline+1 (no second row) and content is the ACCUMULATED partial
       text of both deltas (proves in-place coalescing, not silent drop or
       replace-by-latest);
    4. after the terminal ``kind="agent"`` completion (same chain_id) —
       count STILL stays at baseline+1 (no second entry from the
       completion either) and content is the completion's authoritative
       full text (finalize, not a third partial state).

    Strip-falsify (recorded in the PR body): reverting the ③c consumer
    (``TextualChatApp._handle_agent_delta_event``) to a no-op reproduces
    ③b's old "draws nothing" behavior — step 2 then fails (count stays at
    baseline instead of baseline+1) — confirming this assertion actually
    depends on the ③c coalesce mechanism, not on a tautology.
    """
    transport = QueueTransport()
    clock = _DrivenClock()
    app = TextualChatApp(transport=transport, clock=clock)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        baseline = await _arrival_witness(transport, pilot, app)

        await transport.push_event(
            Event(type="agent_delta", data={"text": "Hello", "chain_id": "c1"})
        )
        await pilot.pause()
        entries = app.query_one(FlowView).entries
        assert len(entries) == baseline + 1, (
            "a single agent_delta must create exactly ONE new flow entry — "
            f"count {baseline} -> {len(entries)}; delta delivery/coalesce is "
            "not creating an entry (possible regression to ③b's pre-③c "
            "'draws nothing' behavior)"
        )
        assert entries[-1].item.text == "Hello", (
            "the first delta's entry must render that delta's own (partial) "
            f"text, got {entries[-1].item.text!r}"
        )

        # #3570: the row is repainted at most once per budget window, so say
        # explicitly that the window has passed rather than hoping the pause
        # outlasted it. The delta's TEXT would accumulate either way.
        clock.past_the_repaint_budget()
        await transport.push_event(
            Event(type="agent_delta", data={"text": ", world", "chain_id": "c1"})
        )
        await pilot.pause()
        entries = app.query_one(FlowView).entries
        assert len(entries) == baseline + 1, (
            "a SECOND delta for the SAME chain_id must coalesce into the "
            f"SAME entry, not append a new row — count changed to {len(entries)}"
        )
        assert entries[-1].item.text == "Hello, world", (
            "the second delta must ACCUMULATE onto the first, not replace "
            f"or drop it, got {entries[-1].item.text!r}"
        )

        await transport.push_display(
            OutboxMessage(kind="agent", text="Hello, world!", meta={"chain_id": "c1"})
        )
        await pilot.pause()
        entries = app.query_one(FlowView).entries
        assert len(entries) == baseline + 1, (
            "the terminal completion for an already-streamed chain_id must "
            f"FINALIZE the existing entry, not append a second one — count "
            f"changed to {len(entries)}"
        )
        assert entries[-1].item.text == "Hello, world!", (
            "the finalized entry must show the completion's AUTHORITATIVE "
            f"full text (L9 whole-persist), got {entries[-1].item.text!r}"
        )


@pytest.mark.asyncio
async def test_agent_delta_ignored_alongside_other_unhandled_events() -> None:
    """Tier 2: non-vacuity companion — a DIFFERENT, genuinely unhandled event
    type (mirroring ``test_textual_chat_app_ignores_non_user_submitted_events_for_flow``)
    still draws NOTHING, proving the opt-in-draw structural property
    (``_pump_frames``'s ``if/elif/.../continue`` with no ``else``) survives
    ③c's addition of an "agent_delta" branch — ③c added ONE new elif arm, it
    did not turn the EVENT path into a default-draw path. Carries its OWN
    arrival witness so this test alone cannot pass vacuously either."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        after_witness = await _arrival_witness(transport, pilot, app)

        await transport.push_event(Event(type="some_future_unhandled_event", data={}))
        await pilot.pause()

        after = len(app.query_one(FlowView).entries)
        assert after == after_witness
