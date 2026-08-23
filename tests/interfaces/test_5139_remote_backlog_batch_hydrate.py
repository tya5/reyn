"""Tier 2: #5139 — a REMOTE backlog batch (server-sent ``MessagesSnapshot``)
hydrates through ONE ``FlowModel.extend`` call, not N individual
``FlowModel.append`` calls (one per :meth:`TextualChatApp._pump_frames`
iteration, the pre-#5139 shape) — and lands ABOVE whatever live frame
arrived in the SAME reconnect/switch burst, never below it. A connect
with literally ZERO frames after the backlog batch itself still shows
history — the batch IS one of the frames :meth:`~reyn.interfaces.
transport.client_transport.ClientTransport.frames` yields, not something
waiting on a NEXT one to trigger it.

Root cause (owner-reported, "起動/attach のたびに全履歴が先頭から流れる"):
``AgUiTransport.frames()`` used to flatten a server-sent
``MessagesSnapshot`` into the SAME per-frame stream every live turn frame
also flows through — N individually-appended rows, N reflows, watched by
a human as a fast top-to-bottom flash.

Fix shape (architect FINAL ruling, issuecomment-5383272756 — supersedes
an earlier side-channel draft this PR briefly carried, reverted before
merge): the backlog is ONE :class:`~reyn.interfaces.transport.frames.
BacklogBatch` item, flowing through the SAME stream every live
:data:`~reyn.interfaces.transport.frames.Frame` does — never a second
channel a caller must separately poll. This is what makes "wire-arrival
order equals apply order" and "a connection with nothing else happening
still delivers its backlog" both hold BY CONSTRUCTION, with nothing else
to prove: the batch is dequeued exactly once, exactly where it sits in
the stream.

A queue-size heuristic ("wait until several frames are buffered, then
batch") was tried and FALSIFIED before this PR (measured:
``suspend_between_frames()`` unconditionally yields to the event loop
after every frame — drain.py:26's own docstring — so a consumer waking
up after one frame typically has NOT had the next one queued yet).

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` (built on
``ClientTransportStub``) — no mocks.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame, Frame
from reyn.runtime.outbox import OutboxMessage

# ``TextualChatApp(transport=...)``'s own defaults (``agent_name="default"``,
# ``self._session_id`` seeded from ``registry.py``'s ``_DEFAULT_SID``) — a
# fixture batch must carry these to be a CURRENT (not stale/discarded) batch
# for a freshly-constructed app that never switches.
_APP_DEFAULT_AGENT = "default"
_APP_DEFAULT_SID = "main"


class _BacklogThenLiveTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport``: :meth:`frames` yields ONE
    :class:`BacklogBatch` first, then the live frames, then ``__end__`` —
    mirroring a real reconnect burst, where the wire-decoded
    ``MessagesSnapshot`` becomes one queued item strictly before the
    first live frame is dequeued (#5139, no side channel)."""

    def __init__(
        self,
        backlog: "list[Frame]",
        live: "list[OutboxMessage]",
        *,
        end: bool = True,
        agent: str = _APP_DEFAULT_AGENT,
        sid: str = _APP_DEFAULT_SID,
    ) -> None:
        self._backlog = list(backlog)
        self._live = list(live)
        self._end = end
        self._agent = agent
        self._sid = sid

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[Frame | BacklogBatch]":
        yield BacklogBatch(agent=self._agent, sid=self._sid, frames=self._backlog)
        for msg in self._live:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(self, text: str, **_kw) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, **_kw) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover - trivial
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _backlog_frames(*texts: str) -> "list[Frame]":
    return [DisplayFrame(OutboxMessage(kind="agent", text=t)) for t in texts]


@pytest.mark.asyncio
async def test_n_backlog_frames_hydrate_through_one_extend_call(monkeypatch) -> None:
    """Tier 2: #5139 witness ① — N backlog frames hydrate through EXACTLY
    ONE ``FlowModel.extend`` call, not N ``FlowModel.append`` calls. The
    witness is the CALL COUNT (machine-countable, per CLAUDE.md's own
    "if it can't be counted, don't put it in acceptance" — not "looks
    fast", which this repo's testing policy does not accept as a witness.

    This file's own fixture (:class:`_BacklogThenLiveTransport`) hand-
    builds the ``BacklogBatch`` item directly — it does not decode SSE
    through the real ``AgUiTransport``, so it cannot witness a regression
    in ``AgUiTransport._consume_block`` itself (that decode step is
    covered by ``tests/interfaces/test_agui_reconnect_snapshot.py`` and
    ``tests/interfaces/test_agui_messages_standard_shape.py``, both
    strip-verified to flip RED when ``_consume_block`` is reverted to
    flatten ``MessagesSnapshot`` into ``out``). What THIS test verifies,
    and strip-falsifies against, is the APP-side half: stubbing
    :meth:`TextualChatApp._apply_backlog_batch` to a no-op turns every
    test in this file RED (verified locally) — the batch-to-``extend``
    wiring, not the wire decode."""
    from textual_flowview import FlowModel

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    extend_calls: "list[list]" = []
    real_extend = FlowModel.extend

    def _counting_extend(self, items):
        extend_calls.append(list(items))
        return real_extend(self, items)

    monkeypatch.setattr(FlowModel, "extend", _counting_extend)

    backlog = _backlog_frames("earlier reply 1", "earlier reply 2", "earlier reply 3")
    transport = _BacklogThenLiveTransport(
        backlog, [OutboxMessage(kind="agent", text="live reply")],
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

    # #3476 ②'s own local-restore batching ALSO calls extend (for an
    # empty/None initial history here) — the property under test is
    # specifically that the 3-frame REMOTE backlog above landed as ONE
    # call carrying ALL THREE together, not 3 separate calls of 1 each.
    # Asserted on the actual CONTENT of each call (not a bare count), so
    # this also proves nothing got split/reordered/duplicated across
    # calls — a stronger witness than a size check alone.
    backlog_calls = [
        [m.text for m in c] for c in extend_calls
        if any(m.text == "earlier reply 1" for m in c)
    ]
    assert backlog_calls == [
        ["earlier reply 1", "earlier reply 2", "earlier reply 3"],
    ], (
        f"expected the 3 backlog frames to land in exactly ONE extend "
        f"call, together, in order; got {backlog_calls!r} (all extend "
        f"calls: {[[m.text for m in c] for c in extend_calls]!r})"
    )


@pytest.mark.asyncio
async def test_backlog_renders_above_a_live_frame_in_the_same_burst(monkeypatch) -> None:
    """Tier 2: #5139 witness ② (architect co-vet, issuecomment-5383105435)
    — the asymmetry ``StateUpdate`` does not have: backlog carries
    POSITION. When a backlog batch and a live frame arrive in the SAME
    burst, the backlog entries must be applied BEFORE the live one — so
    they render ABOVE it, never below (the owner's own reported symptom
    inverted: history flashing OVER the latest turn is what a missing
    barrier would produce)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    backlog = _backlog_frames("earlier turn")
    transport = _BacklogThenLiveTransport(
        backlog, [OutboxMessage(kind="agent", text="the newest live turn")],
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        texts = [e.item.text for e in app.conversation.entries]

    assert texts.index("earlier turn") < texts.index("the newest live turn"), (
        f"the backlog entry must render ABOVE the live entry that arrived "
        f"in the same burst — got order {texts!r}"
    )


@pytest.mark.asyncio
async def test_backlog_shows_with_zero_frames_after_it(monkeypatch) -> None:
    """Tier 2: #5139 witness ④ (lead-coder/architect acceptance,
    issuecomment-5383243289 family) — a connect whose backlog batch is
    followed by literally ZERO further frames (never even an ``__end__``)
    still shows history. #5050 ③'s own measured finding: "a fresh AG-UI
    connect carrying only STATE_SNAPSHOT + no further activity yields
    ZERO frames from frames(), ever" — the pre-#5139 side-channel draft
    needed a SEPARATE fallback worker keyed off ``state_ready()`` for
    exactly this case, because a side-channelled batch never reached
    ``out`` at all when nothing else did either. Under the queue design
    the batch itself is a real, dequeued item — this test drives the
    fixture's ``frames()`` to ACTUALLY yield zero items after it (not a
    vacuous/empty-condition green — the fixture's own ``end=False,
    live=[]`` makes its async generator return immediately after the one
    batch item, a real empty tail, not merely an untested one)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    backlog = _backlog_frames("only entry, no live frame ever follows")
    transport = _BacklogThenLiveTransport(backlog, [], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        texts = [e.item.text for e in app.conversation.entries]

    assert "only entry, no live frame ever follows" in texts, (
        f"the backlog batch must be applied even though frames() yielded "
        f"nothing after it — got {texts!r}"
    )


@pytest.mark.asyncio
async def test_stale_destination_backlog_is_discarded_and_counted() -> None:
    """Tier 2: #5139 witness ①+② combined (architect ruling,
    issuecomment-5383251430) — a batch whose ``(agent, sid)`` does NOT
    match this app's own current location is discarded, not rendered
    ("「在るか」は消失の witness になりません — 「どれが/いくつ」を訊く"),
    and the discard is COUNTED on a public read
    (:meth:`TextualChatApp.remote_backlog_discard_count`) — "0 discards"
    must be verifiable, not merely assumed."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    stale_backlog = _backlog_frames("a stale batch for a different destination")
    transport = _BacklogThenLiveTransport(
        stale_backlog,
        [OutboxMessage(kind="agent", text="live reply")],
        agent="some-other-agent",
        sid="some-other-sid",
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        texts = [e.item.text for e in app.conversation.entries]

    assert "a stale batch for a different destination" not in texts, (
        f"a batch for a DIFFERENT (agent, sid) must never render — got {texts!r}"
    )
    assert app.remote_backlog_discard_count() == 1, (
        f"expected exactly one counted discard; got "
        f"{app.remote_backlog_discard_count()}"
    )
