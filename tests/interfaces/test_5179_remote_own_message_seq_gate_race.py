"""Tier 2: #5179 — in ``--connect`` remote mode, the operator's OWN sent
message can silently never appear in the conversation, while the agent's
reply renders fine.

Real chain (prior investigation, summarized in the issue thread):

- The operator's own message is NEVER folded into the flow directly.
  ``TextualChatApp._handle_user_submitted_event`` only STAGES it into
  ``RemoteQueueView`` (the sent-queue region); a LATER ``turn_started``
  whose ``chain_id`` matches is what actually PROMOTES it into a flow
  entry (``_handle_turn_started_event`` → ``_ingest_frame``). Both calls
  are gated by ``RemoteQueueView``'s own monotonic ``seq`` gate
  (``apply_user_submitted``/``apply_turn_started`` — state.py): a delta
  whose ``seq`` is ``<=`` the view's ``_last_seq`` is silently rejected —
  AND, independent of the gate, ``_handle_turn_started_event`` only ever
  promotes items already sitting in ``RemoteQueueView.queue()``: an item
  whose OWN ``apply_user_submitted`` was gated never got staged, so there
  is nothing to promote even if the ``turn_started`` delta's own gate
  happens to pass.
- ``_last_seq`` is seeded exactly ONCE per connection, from a live read of
  ``transport.status.values`` (``RemoteReadModel.snapshot()`` — read_model.py
  — reads it live, no buffering), at ``TextualChatApp._seed_queue_view``,
  itself called on the FIRST non-``BacklogBatch`` frame ``_pump_frames``
  ever drains from ``transport.frames()``.
- ``AgUiTransport._consume_block`` applies a decoded ``STATE_SNAPSHOT``/
  ``STATE_DELTA`` to ``self._status`` SYNCHRONOUSLY, as soon as that block
  is parsed. A ``STATE_*`` block decodes to ZERO ``Frame``/``BacklogBatch``
  items (see ``_consume_block``'s own branch for ``StateUpdate``), so
  applying it never puts anything onto ``AgUiTransport``'s own frame
  queue — it is invisible to ``frames()``'s consumer except through its
  side effect on ``self._status``.

The construction below (verified directly, not assumed — see
``_build_race_sse``'s own docstring for the measurement): the connection's
ONE-TIME reconnect ``STATE_SNAPSHOT`` (``AgUiEmitter._reconnect_snapshot_
chunks``, built ONCE before ``AgUiEmitter.stream()`` ever starts iterating
its frame source) is computed from a ``status_provider()`` that already
reflects the operator's OWN turn as fully dispatched (``queue_seq`` past
both its ``user_submitted`` and ``turn_started`` deltas' own ``seq``
stamps) — modeling a session whose enqueue+dispatch (one synchronous call)
already completed by the time this particular connection's reconnect
snapshot was computed, while the corresponding ``user_submitted``/
``turn_started`` AUDIT-EVENTS for that SAME turn are still forwarded
afterward over ``AgUiEmitter``'s separate frame-source pipeline, carrying
their own historical ``seq`` stamps (1, 2). Because this STATE_SNAPSHOT
sits on the wire strictly BEFORE either of those two frames (it is part of
the connect preamble, emitted before the frame-source loop even starts)
and decodes to zero queued items, it is GUARANTEED (no scheduling luck
needed — verified below by direct instrumentation, not inferred) to have
already updated ``transport.status`` by the time ``_pump_frames`` drains
the FIRST real frame this connection ever produces — which is exactly
when ``_seed_queue_view`` runs.

An earlier construction this investigation also tried and DID NOT
reproduce the bug is recorded in ``_build_race_sse``'s own docstring,
because the negative result is itself part of the finding (this repo's
own pre-conclusion checklist: report what falsifies a hypothesis, not
only what confirms it).

Real ``AgUiEmitter`` (server-side encoder) + real ``AgUiTransport``
(client-side decoder) + a real mounted ``TextualChatApp`` wired to a real
``RemoteReadModel`` — no mocks, no private-state pokes. The verdict is
read off PUBLIC widget surfaces only: ``FlowView.entries`` (mirroring
``test_3300_p2b_sentqueue_render.py``'s own ``_flow_user_entries``) and
``SentQueue.rendered_texts()``.

**Scope correction, written after the fix landed (endpoint.py/emitter.py,
#5179):** this test builds ``AgUiEmitter`` DIRECTLY with a hand-set
``status_provider`` already reflecting the post-dispatch ``queue_seq`` from
the very first line of its construction — it never calls through
``session_backlog_page``/``endpoint.py`` at all, so it does not exercise
(and cannot be closed by) the endpoint-level fix
(``_session_backlog_page_and_status`` pairing backlog+status in one tick).
It demonstrates a different, narrower thing, correctly: the seq-gate
mechanism itself (``RemoteQueueView``) WILL drop an already-reflected turn
when handed a status that is inconsistent with the frames it is paired
with — which is its designed behavior, not a bug, and stays true forever
(feeding it deliberately-inconsistent inputs will always reproduce this).
This test was earlier mis-described (issue thread) as one of two
acceptance criteria the fix must flip green — that was wrong; the real
end-to-end acceptance coverage for whether the FIXED endpoint actually
avoids producing this inconsistency lives in
``test_5179_backlog_gap_end_to_end.py`` (drives the real, fixed connect
pairing) and ``test_5179_exit_b_status_race_discriminator.py`` (pins the
specific race the fix closes). This file is kept as a mechanism
illustration only and is expected to keep reproducing indefinitely.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.activity_row import ActivityRow
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.repl.read_model import RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import EventFrame
from reyn.schemas.models import Event

_MSG_ID = "m1"
_CHAIN_ID = "c1"
_OWN_TEXT = "hello from the operator"


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


async def _sse_lines(text: str) -> AsyncIterator[str]:
    """Yields ``text``'s own lines then hangs on a real never-resolving
    primitive — a genuinely open connection with nothing further to send
    yet, NOT an exhausted stream (an exhausted finite generator would make
    ``AgUiTransport.frames()`` raise ``StopAsyncIteration`` on its very
    first ``__anext__()`` here, which tears the whole app down via
    ``_pump_frames``'s own ``finally: self.exit()`` before this test ever
    gets to inspect anything — matches
    ``test_5050_remote_pending_intervention_choices.py``'s own
    ``_sse_lines_then_hang`` for the identical reason).

    A real ``asyncio.sleep(0)`` between every yielded line — NOT only
    after the whole block — matches that same helper: a genuine SSE
    source yields control at every line boundary (a real socket read),
    so this is a representative sample of the wire's real timing, not an
    artificial single burst."""
    for line in text.split("\n"):
        yield line
        await asyncio.sleep(0)
    await asyncio.Event().wait()


async def _wait_until(pilot, condition) -> None:
    """Poll ``pilot.pause()`` unboundedly until ``condition()`` is true —
    CLAUDE.md's Ceiling rule: wait on the real condition, never a fixed
    pause count. CI's own ``--timeout`` is the kill switch if it never
    resolves."""
    while not condition():
        await pilot.pause()


async def _build_race_sse() -> str:
    """Builds the exact SSE text via the REAL ``AgUiEmitter``.

    ``status_state`` already carries the post-dispatch counters (``queue_
    seq=2``) BEFORE ``AgUiEmitter`` is even constructed — this is what
    ``_reconnect_snapshot_chunks`` (emitter.py) reads to build the ONE-TIME
    connect ``STATE_SNAPSHOT``, computed before ``stream()`` ever starts
    iterating ``frames``. The ``user_submitted``/``turn_started`` events
    below are the SAME turn's own historical audit-events, forwarded
    afterward with their own ``seq`` stamps (1, 2) — unchanged by the
    snapshot already existing ahead of them, exactly as a real forwarder
    lagging behind a live status read would behave.

    Directly measured (this investigation's own instrumented run, not
    assumed): with THIS construction, ``AgUiTransport``'s decoded
    ``STATUS apply_snapshot`` (queue_seq=2) lands strictly before either
    the ``user_submitted`` or ``turn_started`` ``EventFrame`` is even
    dequeued from ``AgUiTransport.frames()`` — because the STATE_SNAPSHOT
    block sits earlier on the wire and decodes to zero queued items, this
    holds with no scheduling assumption.

    A DIFFERENT construction was tried first and did NOT reproduce the
    bug: mutating ``status_state`` to the post-dispatch values only
    between yielding the ``user_submitted`` and ``turn_started`` frames
    (so the STATE_DELTA reflecting the jump appears mid-stream, between
    the two event frames, exactly as ``AgUiEmitter.stream()``'s real code
    naturally emits — event first, its post-frame delta right after).
    Measured directly: in that construction, ``AgUiTransport.frames()``'s
    OWN per-dequeued-frame ``suspend_between_frames()`` call (on TOP of
    the one inside ``AgUiTransport._pump_sse``) resynchronizes the two
    tasks at every frame boundary, so ``_pump_frames`` always finished
    fully handling frame N (a synchronous call, ``_seed_queue_view``
    included) before ``_pump_sse`` ever got back control to decode block
    N+1's STATE_DELTA. So a delta racing a frame it was itself computed
    AFTER did not reproduce; a snapshot that already raced ahead of a turn
    BEFORE that turn's own frames were ever produced (this module's actual
    construction, above) does."""
    status_state: dict = {"queue": [], "turn_active": True, "queue_seq": 2}

    def status_provider() -> dict:
        return dict(status_state)

    async def frames():
        yield EventFrame(
            Event(
                type="user_submitted",
                data={
                    "msg_id": _MSG_ID, "chain_id": _CHAIN_ID,
                    "text": _OWN_TEXT, "seq": 1, "meta": {},
                },
            )
        )
        yield EventFrame(
            Event(type="turn_started", data={"chain_id": _CHAIN_ID, "seq": 2}),
        )

    emitter = AgUiEmitter(frames(), status_provider)
    chunks = [chunk async for chunk in emitter.stream()]
    return "".join(chunks)


@pytest.mark.asyncio
async def test_own_message_race_construction_outcome() -> None:
    """Tier 2: constructs the exact interleaving #5179 hypothesizes and
    reports the actual, observed outcome — reproduced or not — off public
    widget state only (see module docstring for the full mechanism)."""
    sse = await _build_race_sse()
    # Sanity on the WIRE CONTENT itself (not yet the app): the connect-time
    # STATE_SNAPSHOT really does carry the post-dispatch queue_seq, and it
    # really does sit BEFORE either event frame on the wire.
    snap_pos = sse.index("STATE_SNAPSHOT")
    us_pos = sse.index("user_submitted")
    ts_pos = sse.index("turn_started")
    assert snap_pos < us_pos < ts_pos, (
        "test construction error: expected STATE_SNAPSHOT -> "
        "EVENT(user_submitted) -> EVENT(turn_started) on the wire; got a "
        "different order, so this run does not exercise the race this "
        "test targets"
    )
    assert '"queue_seq": 2' in sse.split("STATE_SNAPSHOT", 1)[1].split("\n\n", 1)[0], (
        "test construction error: the connect-time STATE_SNAPSHOT does not "
        "carry the post-dispatch queue_seq — this run does not exercise "
        "the race this test targets"
    )

    async def _send(_payload: dict) -> bool:
        return False

    transport = AgUiTransport(_sse_lines(sse), _send)
    app = TextualChatApp(transport=transport, read_model=RemoteReadModel(transport))

    async with app.run_test(size=(100, 30)) as pilot:
        # A public, unconditional side effect of ``_handle_turn_started_
        # event`` firing at all (``self._activity.begin("WORKING")`` runs
        # BEFORE the seq gate is even consulted) — waiting on it proves
        # the turn_started frame was drained and handled, independent of
        # whether the promotion itself (the thing under test) succeeded.
        await _wait_until(pilot, lambda: app.query_one(ActivityRow).state is not None)
        # Give any already-scheduled promotion work one more full pass.
        await pilot.pause()
        await pilot.pause()

        user_entries = _flow_user_entries(app)
        sent_queue = app.query_one(SentQueue)
        queued_texts = sent_queue.rendered_texts()

        rendered = any(_OWN_TEXT in e.item.text for e in user_entries)
        still_queued = any(_OWN_TEXT in t for t in queued_texts)

        # This is the reproduction: it currently FAILS (#5179 is unfixed).
        # The operator's own message must be present SOMEWHERE — either
        # promoted into the flow, or still visibly staged in the
        # sent-queue region — after its turn_started was drained and
        # handled. Neither is true under this exact construction: it was
        # silently dropped by the seq-gate race (the connect-time
        # STATE_SNAPSHOT already reflecting the post-dispatch queue_seq
        # reached transport.status before _seed_queue_view ran on frame #1).
        assert rendered or still_queued, (
            "REPRODUCED #5179: the operator's own message "
            f"({_OWN_TEXT!r}) is present in NEITHER the flow "
            f"({[e.item.text for e in user_entries]!r}) NOR the "
            f"sent-queue region ({queued_texts!r})."
        )
