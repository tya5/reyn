"""#3283 ③ — a streamed reply's LIVE UPDATES are gated on its row's viewport
state (``FlowView.track_visibility``), so a long conversation whose streaming
reply has been scrolled away costs O(1) model→view updates instead of
O(deltas).

The load-bearing distinction this file pins is between the two things that must
NOT be conflated:

- **The accumulated text is never gated.** Every delta appends to the tracked
  reply text unconditionally, on screen or off. Scroll-away-then-back must show
  the COMPLETE reply, never a truncated one — the failure mode this whole phase
  is judged on.
- **The RENDER is gated.** While the row is off screen no ``Entry.set_item`` is
  issued at all; ``on_show`` replays the accumulated text in ONE update when the
  row scrolls back.

That makes the deferral an OPTIMISATION and the replay a CORRECTNESS leg, and
the gates below are shaped to tell them apart:

- ``test_offscreen_stream_is_complete_when_scrolled_back`` — the completeness
  gate. It passes with OR without the deferral (deferral is not what makes the
  text complete) and goes RED if the ``on_show`` replay is stripped.
- ``test_offscreen_deltas_do_not_re_render_the_row`` — the DISCRIMINATING count.
  ``Entry.revision`` is flowview's public per-entry update counter (its
  presentation cache key, bumped by every ``set_item``), so counting it counts
  the actual update feed on the real path rather than inferring the deferral
  from a terminal state. Note this cannot be witnessed by presenter-present
  counts: flowview ALREADY skips the present+reflow for an off-screen update
  (``FlowView.on_flow_update``), so a present count would read the same with and
  without ③. The revision is the feed, and flowview does not gate the feed.
- ``test_scrolling_back_replays_in_one_update`` — the replay is ONE update, not
  a catch-up of N.
- ``test_offscreen_completion_settles_authoritative_text`` — the terminal
  completion is NOT visibility-gated.
- ``test_session_switch_leaves_no_in_flight_streamed_reply`` — nothing tracked
  (and so no callback registered by this app) survives a session switch.
- ``test_release_is_idempotent_and_fires_on_hide_once`` — the visibility handle's
  release contract, against a REAL mounted ``FlowView``.

All real instances: a real ``TextualChatApp`` under ``run_test``, a real
``ClientTransport`` implementation fed from a queue (the ``QueueTransport``
idiom shared with ``tests/interfaces/test_3288_3c_tui_delta_coalesce.py``), real
``OutboxMessage`` / ``Event`` / ``FlowView`` / ``FlowModel``. No
``unittest.mock``, no hand-rolled stand-ins.
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

#: Rows pushed BELOW the streamed reply to drive it above the fold. Comfortably
#: more than a short test viewport holds, so the reply is genuinely off-screen
#: (asserted via the public ``visible_range()`` in every gate that needs it).
_FILLER_ROWS = 40


class _DrivenClock:
    """The app's own ``clock`` injection point, driven instead of slept through
    (the idiom ``tests/interfaces/test_stream_spinner_3530.py`` uses for the blink).

    Needed here since #3570: the repaint budget reads this clock, so a test that
    wants "one delta, one update" has to say when a delta is due rather than
    race a real 33 ms window."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (the idiom shared with ``tests/interfaces/test_3288_3c_tui_delta_coalesce.py``)."""

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


def _delta(*, chain_id: str, text: str) -> Event:
    return Event(type="agent_delta", data={"text": text, "chain_id": chain_id})


def _completion(*, chain_id: str, text: str) -> OutboxMessage:
    return OutboxMessage(kind="agent", text=text, meta={"chain_id": chain_id})


def _reply_entry(app: TextualChatApp, chain_id: str):
    """The single flow entry a streamed reply coalesced into, found through the
    PUBLIC ``FlowView.entries`` by its ``chain_id`` meta."""
    matches = [
        entry
        for entry in app.query_one(FlowView).entries
        if (entry.item.meta or {}).get("chain_id") == chain_id
    ]
    assert len(matches) == 1, f"expected exactly one entry for {chain_id}: {matches!r}"
    return matches[0]


def _is_offscreen(app: TextualChatApp, entry) -> bool:
    flow = app.query_one(FlowView)
    idx = flow.entries.index(entry)
    start, stop = flow.visible_range()
    return not (start <= idx < stop)


async def _push_reply_above_the_fold(
    transport: QueueTransport, pilot, app: TextualChatApp, chain_id: str, seed: str
):
    """Start a streamed reply, then push enough later rows to drive it above the
    fold, and confirm OFF-SCREEN via the public ``visible_range()``.

    Returns the reply's entry. The precondition assert is what keeps every gate
    built on this helper non-vacuous — a reply that is actually still visible
    would make the deferral gates trivially true."""
    await transport.push_event(_delta(chain_id=chain_id, text=seed))
    await pilot.pause()
    entry = _reply_entry(app, chain_id)
    for i in range(_FILLER_ROWS):
        await transport.push_display(OutboxMessage(kind="agent", text=f"filler {i}"))
    await pilot.pause()
    app.query_one(FlowView).scroll_to_bottom()
    await pilot.pause()
    assert _is_offscreen(app, entry), "precondition: the streamed reply must be off-screen"
    return entry


@pytest.mark.asyncio
async def test_offscreen_stream_is_complete_when_scrolled_back() -> None:
    """Tier 2b: ★the completeness gate — deltas that arrive while the reply's row
    is scrolled OUT OF VIEW are all present when it scrolls back.

    Deferring the render must never defer (or drop) the accumulated text. This
    gate is deliberately insensitive to the deferral itself — with the deferral
    stripped the row is simply updated per delta and the text is equally complete
    — and sensitive to the ``on_show`` replay, which is what actually puts the
    deferred text on screen."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        entry = await _push_reply_above_the_fold(transport, pilot, app, "chain-c", "Hello")

        offscreen_chunks = [" world", ", this", " arrived", " off", " screen."]
        for chunk in offscreen_chunks:
            await transport.push_event(_delta(chain_id="chain-c", text=chunk))
        await pilot.pause()

        # Scroll the reply back into view — the replay leg.
        flow = app.query_one(FlowView)
        flow.scroll_to_entry(entry)
        await pilot.pause()
        await pilot.pause()
        assert not _is_offscreen(app, entry), "the reply should be visible again"

        expected = "Hello" + "".join(offscreen_chunks)
        assert entry.item.text == expected, (
            "scrolling away truncated the streamed reply — the accumulated text "
            f"must survive the deferral: {entry.item.text!r}"
        )
        # ...and the row the user actually reads carries it too, not just the item.
        assert "off screen." in flow.entry_text(entry)


@pytest.mark.asyncio
async def test_offscreen_deltas_do_not_re_render_the_row() -> None:
    """Tier 2b: ★the discriminating count — an off-screen reply is NOT re-rendered
    per delta, while an on-screen one is.

    ``Entry.revision`` is flowview's public update counter, bumped by every
    ``Entry.set_item``; counting it counts the real update feed rather than
    inferring the deferral from the final content (which cannot tell "deferred"
    from "updated every time"). Both legs are asserted in the SAME test so the
    negative is not vacuous: the same code path DOES feed the row while visible.

    #3570 added a SECOND gate in front of the same ``set_item``: a repaint
    budget, so an on-screen row is fed at most once per
    ``_STREAM_REPAINT_MIN_INTERVAL`` however fast deltas arrive. The visible leg
    therefore drives the app's own injected clock past that window between
    deltas — which keeps this test measuring the ③ VISIBILITY gate (its subject)
    at full strength rather than accidentally measuring #3570's budget. The
    budget's own gates live in ``tests/interfaces/test_stream_repaint_coalesce_3570.py``.
    """
    transport = QueueTransport()
    clock = _DrivenClock()
    app = TextualChatApp(transport=transport, clock=clock)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        # Leg 1 — VISIBLE: the feed is live, every (budget-clear) delta bumps
        # the revision.
        await transport.push_event(_delta(chain_id="chain-v", text="a"))
        await pilot.pause()
        visible_entry = _reply_entry(app, "chain-v")
        assert not _is_offscreen(app, visible_entry)
        before_visible = visible_entry.revision
        for _ in range(6):
            clock.advance(_STREAM_REPAINT_MIN_INTERVAL * 2)
            await transport.push_event(_delta(chain_id="chain-v", text="a"))
            await pilot.pause()
        live_bumps = visible_entry.revision - before_visible
        assert live_bumps == 6, (
            f"a VISIBLE streamed reply must be fed every delta, got {live_bumps} of 6"
        )

        # Leg 2 — OFF SCREEN: the feed is deferred, no update is issued at all —
        # and the clock keeps advancing past the budget, so what is measured is
        # the visibility gate alone, not a repaint that was merely not due yet.
        entry = await _push_reply_above_the_fold(transport, pilot, app, "chain-o", "a")
        before = entry.revision
        for _ in range(6):
            clock.advance(_STREAM_REPAINT_MIN_INTERVAL * 2)
            await transport.push_event(_delta(chain_id="chain-o", text="a"))
        await pilot.pause()
        await pilot.pause()
        assert _is_offscreen(app, entry), "the reply must still be off-screen"
        offscreen_bumps = entry.revision - before
        assert offscreen_bumps == 0, (
            "an OFF-SCREEN streamed reply was re-rendered per delta "
            f"({offscreen_bumps} updates for 6 off-screen deltas) — the ③ "
            "visibility gate is not feeding through"
        )


@pytest.mark.asyncio
async def test_scrolling_back_replays_in_one_update() -> None:
    """Tier 2b: the replay is ONE update carrying the whole accumulated text, not
    a catch-up replaying each deferred delta in turn."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        entry = await _push_reply_above_the_fold(transport, pilot, app, "chain-r", "a")
        for _ in range(8):
            await transport.push_event(_delta(chain_id="chain-r", text="b"))
        await pilot.pause()
        before = entry.revision

        app.query_one(FlowView).scroll_to_entry(entry)
        await pilot.pause()
        await pilot.pause()

        bumps = entry.revision - before
        assert bumps == 1, (
            f"scrolling back should replay the deferred text in ONE update, got {bumps}"
        )
        assert entry.item.text == "a" + "b" * 8


@pytest.mark.asyncio
async def test_offscreen_completion_settles_authoritative_text() -> None:
    """Tier 2b: the terminal completion is NOT visibility-gated — an off-screen
    reply is settled with the completion's authoritative full text immediately,
    so nothing depends on the user ever scrolling back."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        entry = await _push_reply_above_the_fold(transport, pilot, app, "chain-s", "part")
        await transport.push_event(_delta(chain_id="chain-s", text="ial"))
        await pilot.pause()
        assert _is_offscreen(app, entry)

        await transport.push_display(_completion(chain_id="chain-s", text="the whole reply"))
        await pilot.pause()

        assert _is_offscreen(app, entry), "precondition: still off-screen at settle time"
        assert entry.item.text == "the whole reply"
        # Exactly ONE entry for this chain_id — the completion settled in place
        # rather than appending a second row (the #3288 ③c contract, unchanged).
        _reply_entry(app, "chain-s")


@pytest.mark.asyncio
async def test_session_switch_leaves_no_in_flight_streamed_reply() -> None:
    """Tier 2b: a session switch drops every in-flight streamed reply — including
    the record that OWNS its visibility callbacks — so a delta arriving after the
    switch with the SAME chain_id starts a FRESH row in the new session's flow,
    seeded only with post-switch text.

    Discriminating: were the tracked record to survive the switch, the
    post-switch delta would accumulate onto the OLD (removed, off-model) entry
    and NOTHING would appear in the new session's flow at all.

    A ``session_attached`` event without a matching read model rehydrates
    nothing (the hydrate call is internally guarded), which is exactly the
    cross-section this gate wants: an EMPTY post-switch flow, so the fresh row
    is unambiguous."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        await transport.push_event(_delta(chain_id="chain-x", text="before switch"))
        await pilot.pause()
        assert _reply_entry(app, "chain-x").item.text == "before switch"

        await transport.push_event(
            Event(type="session_attached", data={"agent": "beta", "session_id": "s-2"})
        )
        await pilot.pause()
        assert app.query_one(FlowView).entries == [], "the switch must clear the flow"

        await transport.push_event(_delta(chain_id="chain-x", text="after switch"))
        await pilot.pause()
        fresh = _reply_entry(app, "chain-x")
        assert fresh.item.text == "after switch", (
            "a post-switch delta for a pre-switch chain_id must seed a FRESH row, "
            f"not accumulate onto the old session's tracking: {fresh.item.text!r}"
        )


@pytest.mark.asyncio
async def test_release_is_idempotent_and_fires_on_hide_once() -> None:
    """Tier 2b: the ``track_visibility`` release contract this phase relies on,
    against a REAL mounted ``FlowView``: stopping a tracker for an on-screen
    entry runs ``on_hide`` exactly once, and stopping it again is a no-op.

    This is what makes ``_StreamingReply.release`` safe to call from BOTH the
    terminal completion and the session-switch reset without double-releasing.
    The counters are plain local lists appended to by real callbacks — no
    stand-in object."""
    shown: "list[object]" = []
    hidden: "list[object]" = []

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        flow = app.query_one(FlowView)
        entry = app.conversation.append(OutboxMessage(kind="agent", text="visible row"))
        await pilot.pause()
        handle = flow.track_visibility(
            entry, on_show=shown.append, on_hide=hidden.append
        )
        # Already on screen, so on_show fired synchronously on registration.
        assert shown == [entry]
        assert hidden == []

        handle.stop()
        assert hidden == [entry], "stopping a shown tracker must release it once"
        handle.stop()
        assert hidden == [entry], "a second stop must be a no-op"
