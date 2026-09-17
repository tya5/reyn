"""Tier 2: #6216 — a streamed reply's TERMINAL completion frame now
carries its OWN `round_index` (#6218 already threads it onto every
`kind="agent"` `put_outbox` call site, for `_call_parent_key`'s sake);
the streaming-settle consumer (`app.py`'s `_pop_streaming_round`, née
`_pop_last_streaming_round`) now settles THAT round, never "whichever
round happens to have the highest index still open".

## The defect this closes (measured, `app.py:5916`'s own retired comment)

> The completion carries no round, so it settles the LAST round of
> this chain — the one whose text it holds.

This was a RECONSTRUCTED invariant ("the completion is always the
current highest round"), never a fact asserted BY a producer —
exactly the shape owner ruling B (`dispatcher.py`'s own "not an
invariant to key UI structure on") forbids. `_close_earlier_streaming_
rounds` (fired on every NEW round's first delta) already pops/releases
any EARLIER round's own streaming record the instant a later round's
first chunk lands — so if an earlier round's own completion frame
ever arrives AFTER that point (a genuinely possible ordering: deltas
and completions are two independent producer emits, not one atomic
write), the old `max()` consumer had nothing left to settle but the
LATER round's still-open record, and would silently overwrite ITS
in-progress text with the EARLIER round's completion text.

## Accept criteria (issue #6216, effect-based)

① a completion frame carries its own round (producer side — #6218
   already lands this for router_loop.py's 4 sites; this stage adds
   `session.py`'s own 2 router-cap force-close sites, verified their
   own real value: `_temp_loop._delta_round_index`, always 0 since
   that fresh RouterLoop's main loop never ran — see
   `tests/runtime/test_router_cap_exhausted.py` and siblings for that
   half; this file covers the CONSUMER half only, using synthetic
   round_index-bearing frames matching the shape #6218 already
   verified real producers emit).
② the same `chain_id` with 2 rounds open settles EACH completion into
   its OWN round, never `max()`.
③ ⭐ the guardian — a completion for a round_index that is NOT the
   currently-highest open round still settles CORRECTLY (into its own
   round, or degrades to a new row if its own round is already gone —
   never corrupting a DIFFERENT, still-open round). This is what turns
   red under the retired `max()` implementation; ② alone would stay
   green even reverted, since "the completion's own round happens to
   also be the max" is the ordinary, unremarkable case.
④ an earlier round's release is unchanged (deny side — releasing a
   stale record on round transition, `_close_earlier_streaming_rounds`,
   is untouched by this fix).

Real `TextualChatApp`/`FlowView`, real `agent_delta` `EventFrame` +
real `kind="agent"` `DisplayFrame` — no mocks, mirrors
`test_agent_delta_round_index_3656.py`'s own established idiom (same
file family, same symptom class, kept local per that file's own
"neither module exports its collaborator" convention).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time —
    same shape as `test_agent_delta_round_index_3656.py`'s own local
    copy (neither module exports its collaborator)."""

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

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> None:  # pragma: no cover
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


_CHAIN = "chain-6216"


def _delta(text: str, round_index: int) -> Event:
    return Event(
        type="agent_delta",
        data={"text": text, "chain_id": _CHAIN, "round_index": round_index},
    )


def _completion(text: str, round_index: int) -> OutboxMessage:
    """A terminal `kind="agent"` completion frame carrying its own
    round — the shape #6218 already produces at every real router_loop.py
    `put_outbox` call site."""
    return OutboxMessage(
        kind="agent", text=text,
        meta={"chain_id": _CHAIN, "round_index": round_index, "finish_reason": "stop"},
    )


def _rows(app: TextualChatApp) -> "list[str]":
    return [
        str(entry.item.text)
        for entry in app.query_one(FlowView).entries
        if (entry.item.meta or {}).get("chain_id") == _CHAIN
    ]


@pytest.mark.asyncio
async def test_a_rounds_completion_settles_its_own_round_not_max() -> None:
    """Tier 2b: accept② — two rounds each settle into their OWN entry
    via their OWN completion, in the ORDINARY (in-order) arrival shape:
    round 1 streams+completes, THEN round 2 streams+completes."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        await transport.push_event(_delta("round one text", round_index=1))
        await pilot.pause()
        await transport.push_display(_completion("round one FINAL", round_index=1))
        await pilot.pause()

        await transport.push_event(_delta("round two text", round_index=2))
        await pilot.pause()
        await transport.push_display(_completion("round two FINAL", round_index=2))
        await pilot.pause()

        rows = _rows(app)
        assert any(row == "round one FINAL" for row in rows), rows
        assert any(row == "round two FINAL" for row in rows), rows


@pytest.mark.asyncio
async def test_a_stale_rounds_late_completion_does_not_corrupt_the_open_round() -> None:
    """Tier 2b: accept③ — the guardian. Round 1 streams, round 2's FIRST
    delta arrives (closing round 1's own streaming record via
    `_close_earlier_streaming_rounds`, before round 1's own completion
    has shown up at all) — THEN round 1's completion arrives late.

    Strip-falsify witness: under the retired `max()` consumer, round 1's
    late completion would find round 2 as the ONLY open record and
    OVERWRITE round 2's still-in-progress text with round 1's completion
    text. The fix must instead leave round 2's entry untouched and give
    round 1's completion its own, separate row (its own round's record
    is already gone — the same "no match → ordinary new row" degrade
    #6214 established, not a new fallback)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        await transport.push_event(_delta("round one streaming", round_index=1))
        await pilot.pause()
        # Round 2's first delta -- closes round 1's OWN streaming record
        # (_close_earlier_streaming_rounds) before round 1's completion
        # has arrived.
        await transport.push_event(_delta("round two streaming", round_index=2))
        await pilot.pause()
        # Round 1's completion, now LATE.
        await transport.push_display(_completion("round one FINAL", round_index=1))
        await pilot.pause()

        rows = _rows(app)
        assert "round one FINAL" in rows, (
            f"round 1's late completion must still land, as its own row: {rows!r}"
        )
        # The load-bearing assertion: round 2's own still-open entry must
        # NOT have been overwritten with round 1's completion text.
        assert any("round two streaming" in row for row in rows), (
            f"round 2's own in-progress text was corrupted by round 1's "
            f"late completion: {rows!r}"
        )
        assert not any("round two streaming" in row and "round one FINAL" in row for row in rows), (
            f"round 1's completion text leaked into round 2's own entry: {rows!r}"
        )


@pytest.mark.asyncio
async def test_earlier_round_release_is_unchanged() -> None:
    """Tier 2b: accept④ (deny side) — a round transition still releases
    the earlier round's own streaming record (unchanged mechanism,
    `_close_earlier_streaming_rounds`, untouched by this fix) — its text
    stays on screen, it simply stops accepting further deltas or a
    later settle."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        await transport.push_event(_delta("round one", round_index=1))
        await pilot.pause()
        await transport.push_event(_delta("round two", round_index=2))
        await pilot.pause()

        rows = _rows(app)
        assert any("round one" in row for row in rows), (
            f"an earlier round's text must survive its own release: {rows!r}"
        )
        assert any("round two" in row for row in rows)
