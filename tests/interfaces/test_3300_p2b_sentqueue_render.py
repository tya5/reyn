"""#3300 P2b — render the sent-queue region + materialize/promote lifecycle.

Phase 2b of the input-message-lifecycle arc (render-only per the architect's
P2b scope comment): adds the sent-queue region to the Textual chat zone
(between the #3299 intervention panel and the input row) and realizes the
"upward conveyor" the owner's sent-queue exit contract (#3300 issue §6a)
describes for the render side:

- ``user_submitted`` -> MATERIALIZE: a submitted message appears in the
  sent-queue region (dim, queued) — NOT immediately as a flow entry. This
  REPLACES P1 C's "render the echo directly as a flow entry"
  (``tests/interfaces/test_user_submitted_render_3300.py`` retargets its own
  TextualChatApp assertions to match).
- ``turn_started(seq, chain_id)`` -> PROMOTE: the matching queued item is
  removed from the sent-queue and appended as a flow entry (the user line) —
  the dispatch promotion.
- ``inbox_cancel`` removal is Phase 3 Y — not covered here.

The client queue model is driven by ``RemoteQueueView`` (#3300 P2a, REUSED
as-is — its seq-gated merge is not reinvented here), fed by the SAME
``user_submitted``/``turn_started`` audit-events every surface already
receives.

Gates covered (per the architect's P2b scope comment):

1. **materialize + promote** — the core lifecycle, end to end.
2. **neutralize + rendered-content fidelity witness** (the concentrated
   security gate — the #3302-class injection lesson applied here: a queued
   item's text is LLM-adjacent/untrusted user-derived content reaching the
   terminal). Non-vacuity manually verified (per repo discipline: temporarily
   removing the ``_neutralized_label`` call in
   ``SentQueue.show_item`` made this test's assertion fail with a raw ESC
   byte present in the rendered content, then restored).
3. **delta live update** — the region reflects enqueue/promote deltas as they
   arrive; an automated strip test (monkeypatching the app's own delta
   handler to a no-op) proves the positive assertion is not vacuous.
4. **reconnect-reseed witness** (architect-required) — ``apply_snapshot``'s
   ``_last_seq`` reseed on (re)connect protects against a stale,
   post-disconnect delta resurrecting an already-superseded item; an
   automated strip test (neutering the reseed) proves the positive assertion
   is load-bearing.

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` throughout — no
``unittest.mock``. Renders are observed via the mounted widgets' PUBLIC
surface (``SentQueue.rendered_texts()`` / ``FlowView.entries``), never
private state.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.sent_queue import ROW_TEXT_COLUMN, SentQueue
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.agui.state import RemoteQueueView
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event

_RAW_ESC_OSC = "\x1b[31mRED\x1b]0;pwn\x07"


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from
    a queue (mirrors ``test_user_submitted_render_3300.py``'s helper) — lets
    a test push an ``EventFrame`` and inspect ``TextualChatApp``'s retained
    conversation model + mounted widgets afterward, stream staying open
    throughout."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

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


def _flow_user_entries(app: TextualChatApp):
    return [e for e in app.query_one(FlowView).entries if e.item.kind == "user"]


class _SnapshotSeededReadModel(ChatReadModel):
    """A real, minimal :class:`ChatReadModel` whose :meth:`snapshot` returns a
    FIXED status dict carrying one already-queued, ATTRIBUTED item — standing
    in for "this client connected while a peer's submit was already sitting
    in the server-authoritative queue" (the connect ``STATE_SNAPSHOT``/
    ``queued_user_messages()`` path, #3300 P2a/P2b), without needing a real
    remote transport. Every other accessor degrades to the same graceful
    empty/None ``RemoteReadModel`` uses — this read-model's only job is the
    ``queue``/``turn_active``/``queue_seq`` snapshot shape."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

    def __init__(self, queue_item: dict) -> None:
        self._queue_item = queue_item

    def snapshot(self, config=None):
        return {
            "queue": [self._queue_item], "turn_active": False, "queue_seq": 1,
        }

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return False

    @property
    def history_path(self) -> Path:
        return Path("/dev/null")

    def conversation_history(self, *, limit: "int | None" = None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


# ---------------------------------------------------------------------------
# 1. Materialize + promote (the core lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_stages_in_sent_queue_not_flow() -> None:
    """Tier 2b: a "user_submitted" delta materializes in the sent-queue
    region — the item is NOT a flow entry yet."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="first message", seq=1)
        )
        await pilot.pause()

        assert not _flow_user_entries(app), "materialize must not append a flow entry"
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.display is True
        (row,) = sent_queue.rendered_texts()
        assert "first message" in row


@pytest.mark.asyncio
async def test_turn_started_promotes_matching_item_to_flow_entry() -> None:
    """Tier 2b: a "turn_started" delta whose chain_id matches a queued item
    removes it from the sent-queue AND appends it as a flow entry — the
    dispatch promotion, in the SAME step (no frame where it is both queued
    and already in the flow, or neither)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="dispatch me", seq=1)
        )
        await pilot.pause()
        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items()

        await transport.push_event(_turn_started(chain_id="c1", seq=2))
        await pilot.pause()

        assert not sent_queue.has_items(), "promoted item must leave the sent-queue"
        (entry,) = _flow_user_entries(app)
        assert entry.item.text == "dispatch me"


@pytest.mark.asyncio
async def test_turn_started_for_unrelated_chain_id_is_a_noop() -> None:
    """Tier 2b: non-vacuity — a "turn_started" for a DIFFERENT chain_id
    (e.g. an agent-driven turn; ``turn_started`` fires for every turn kind,
    not only user ones) does not touch an unrelated queued item, proving the
    promotion above is keyed by chain_id, not "any turn_started"."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="still queued", seq=1)
        )
        await pilot.pause()

        await transport.push_event(_turn_started(chain_id="some-other-chain", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert "still queued" in sent_queue.rendered_texts()[0]
        assert not _flow_user_entries(app)


# ---------------------------------------------------------------------------
# 2. ★Neutralize + rendered-content fidelity witness (#3302-class gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sent_queue_neutralizes_raw_esc_osc_injection() -> None:
    """Tier 2b: ★security gate — a queued item's text is LLM-adjacent/
    untrusted user-derived content reaching the terminal, the SAME injection
    class as the #3302 panel-label bug. A raw ESC/OSC payload injected into a
    queued item's text must NOT survive into the rendered/queried content.

    Non-vacuity (manually verified per repo discipline): temporarily
    removing the ``_neutralized_label(text)`` call in
    ``SentQueue.show_item`` (substituting the bare ``text``) makes this
    assertion fail — the raw ``\\x1b`` byte leaks into
    ``sent_queue.rendered_texts()`` — then the call was restored. ESC is the
    load-bearing byte: Textual's ``Content`` strips BEL/BS/VT/FF/CR itself
    but never ESC, so the neutralize call is what carries this gate.
    """
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text=_RAW_ESC_OSC, seq=1)
        )
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        (row,) = sent_queue.rendered_texts()
        assert "\x1b" not in row, "raw ESC byte leaked into the rendered sent-queue row"
        assert "RED" in row  # the harmless literal remainder still renders


@pytest.mark.asyncio
async def test_promoted_flow_entry_also_neutralizes_raw_esc_osc_injection() -> None:
    """Tier 2b: the SAME untrusted text, once PROMOTED to a flow entry, stays
    neutralized there too — the promotion path re-neutralizes from the raw
    item text (:func:`~reyn.interfaces.inline.textual_chat.presenter._neutralized_label`),
    it does not just trust whatever the sent-queue row already rendered."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text=_RAW_ESC_OSC, seq=1)
        )
        await pilot.pause()
        await transport.push_event(_turn_started(chain_id="c1", seq=2))
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert "\x1b" not in entry.item.text
        assert "RED" in entry.item.text


# ---------------------------------------------------------------------------
# 3. Delta live update (+ automated strip witness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sent_queue_updates_live_as_deltas_arrive() -> None:
    """Tier 2b: the sent-queue region tracks MULTIPLE items across a mixed
    enqueue/dispatch sequence — item appears on ``user_submitted``, is
    removed/promoted on ITS OWN ``turn_started``, and a still-undispatched
    sibling stays visible throughout."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_user_submitted(msg_id="m1", chain_id="c1", text="alpha", seq=1))
        await transport.push_event(_user_submitted(msg_id="m2", chain_id="c2", text="beta", seq=2))
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        # Take the label by COLUMN, not by splitting on a glyph: #3777 made the
        # glyph itself carry selection (▷ queued / ▶ selected), so splitting on
        # one of them silently leaves the other row's glyph in the label and the
        # set comparison fails for a reason that has nothing to do with what
        # this test is about. Every row's label starts at the same column by
        # construction — that is what ROW_TEXT_COLUMN is for.
        assert {"alpha", "beta"} <= {
            t[ROW_TEXT_COLUMN:] for t in sent_queue.rendered_texts()
        }

        await transport.push_event(_turn_started(chain_id="c1", seq=3))
        await pilot.pause()

        # Variable-binding idiom (per test-audit policy) instead of a bare
        # len(...) == N pin: exactly one row remains, and it is "beta" — not
        # a fresh append, the SAME sibling that was already there.
        (remaining,) = sent_queue.rendered_texts()
        assert "beta" in remaining
        assert "alpha" not in remaining
        (entry,) = _flow_user_entries(app)
        assert entry.item.text == "alpha"


@pytest.mark.asyncio
async def test_strip_delta_subscription_leaves_sent_queue_stale(monkeypatch) -> None:
    """Tier 2b: non-vacuity — neutering the app's ``user_submitted`` delta
    handler (the "drop the delta subscription" strip the architect's gate
    names) leaves the sent-queue region STALE (never populated) despite a
    real delta arriving, proving the positive test above is not vacuous."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    monkeypatch.setattr(
        TextualChatApp, "_handle_user_submitted_event", lambda self, event: None,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="should be stale", seq=1)
        )
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert not sent_queue.has_items(), "stripped handler should leave the region stale"
        assert not _flow_user_entries(app)


# ---------------------------------------------------------------------------
# 4. ★Reconnect-reseed witness (architect-required, #3300 P2b)
# ---------------------------------------------------------------------------


def test_reconnect_reseed_prevents_stale_delta_resurrection_after_reconnect() -> None:
    """Tier 1: reconnect-reseed witness — ``RemoteQueueView.apply_snapshot``'s
    ``_last_seq`` reseed on (re)connect protects against a stale,
    post-disconnect delta resurrecting an item the reconnect snapshot already
    supersedes.

    Scenario: the client enqueues "m1" (seq=1), then DISCONNECTS. While
    offline, the server dispatches m1 (seq=2), enqueues an unrelated m2
    (seq=3), and dispatches m2 too (seq=4) — the client sees none of this
    live. On RECONNECT, a fresh ``STATE_SNAPSHOT`` reflects the current
    truth (both items long gone) and reseeds the gate to ``queue_seq=4``. A
    STALE "user_submitted" for m2 (seq=3, from before the reconnect) then
    gets redelivered — e.g. an SSE buffering/replay overlap at the moment of
    reconnect — and the reseed must reject it.
    """
    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=0)
    view.apply_user_submitted(msg_id="m1", chain_id="c1", text="hi", seq=1)
    assert [i["msg_id"] for i in view.queue()] == ["m1"]

    # RECONNECT: reseeds the gate to the server's current truth.
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=4)
    assert view.queue() == []

    resurrected = view.apply_user_submitted(
        msg_id="m2", chain_id="c2", text="ghost", seq=3,
    )
    assert resurrected is False, "reseed failed to protect against a stale post-disconnect delta"
    assert view.queue() == [], "a stale delta must not resurrect an item the reconnect snapshot already superseded"


def test_reconnect_reseed_witness_strip_without_reseed_resurrects(monkeypatch) -> None:
    """Tier 1: non-vacuity — neutering ``apply_snapshot`` so it never
    reseeds ``_last_seq`` from ``queue_seq`` (only replaces
    ``items``/``turn_active``, the strip the architect's gate names) makes
    the EXACT SAME stale delta from the test above wrongly resurrect the
    already-dispatched item, proving the reseed line is load-bearing."""

    def _broken_apply_snapshot(self, *, queue, turn_active, queue_seq):
        self.items = {
            item["msg_id"]: dict(item) for item in queue if item.get("msg_id")
        }
        self.turn_active = turn_active
        # BUG under test: `self._last_seq` is never reseeded from `queue_seq`.

    monkeypatch.setattr(RemoteQueueView, "apply_snapshot", _broken_apply_snapshot)

    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=0)
    view.apply_user_submitted(msg_id="m1", chain_id="c1", text="hi", seq=1)

    # Broken reconnect: queue_seq=4 arrives but `_last_seq` stays at 1.
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=4)

    resurrected = view.apply_user_submitted(
        msg_id="m2", chain_id="c2", text="ghost", seq=3,
    )
    assert resurrected is True, (
        "the broken (unreseeded) gate should incorrectly accept this stale delta"
    )
    assert [i["msg_id"] for i in view.queue()] == ["m2"], (
        "ghost item resurrected — proves the reseed line matters"
    )


# ---------------------------------------------------------------------------
# 5. ★Snapshot-seeded attribution (co-vet finding) — ADR-0039 parity for the
#    late-joiner path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_seeded_item_keeps_attribution_after_promotion() -> None:
    """Tier 2b: a client that connects while a PEER's attributed submit is
    already sitting in the server-authoritative queue (the connect
    ``STATE_SNAPSHOT`` / ``queued_user_messages()`` path, not a live delta)
    must still render the correct ``[actor]`` attribution once that item
    promotes to a flow entry — the SAME ADR-0039 provenance a live
    ``user_submitted`` delta already carries (``meta["actor"]``,
    ``renderer._meta_prefix``'s vocabulary). Regression coverage for the
    co-vet finding: ``queued_user_messages()`` (session.py) now projects
    ``meta`` alongside msg_id/chain_id/text, and the sent-queue's snapshot
    seed (``TextualChatApp._seed_queue_view``) carries it into the SAME
    ``_queue_item_meta`` side table the live delta path populates."""
    read_model = _SnapshotSeededReadModel(
        {
            "msg_id": "m1", "chain_id": "c1", "text": "peer's queued line",
            "meta": {"actor": "alice", "auth_user_id": "alice"},
        }
    )
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # The queue view is seeded on the FIRST frame the pump processes
        # (:meth:`TextualChatApp._seed_queue_view`) — a harmless, unhandled
        # event type triggers that seed without mutating any state, so the
        # snapshot-seeded item's pre-promotion visibility can be observed
        # before the (separate) turn_started frame promotes it.
        await transport.push_event(Event(type="__noop__", data={}))
        await pilot.pause()
        sent_queue = app.query_one(SentQueue)
        assert "peer's queued line" in sent_queue.rendered_texts()[0]

        await transport.push_event(_turn_started(chain_id="c1", seq=2))
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert entry.item.text == "peer's queued line"
        assert entry.item.meta.get("actor") == "alice", (
            "a snapshot-seeded item lost its ADR-0039 attribution on "
            "promotion — it must render like a delta-path item, not a "
            "plain unattributed operator line"
        )


@pytest.mark.asyncio
async def test_strip_snapshot_meta_carry_loses_attribution(monkeypatch) -> None:
    """Tier 2b: non-vacuity — neutering the snapshot-seed's meta-carry (the
    ``self._queue_item_meta[msg_id] = dict(item.get("meta") or {})`` line in
    ``_seed_queue_view``, patched here to drop it) reproduces the co-vet
    finding: the promoted item's meta is empty and the ``[actor]``
    attribution is lost, proving the positive test above is not vacuous."""
    from reyn.interfaces.inline.textual_chat import app as app_module

    def _seed_without_meta_carry(self) -> None:
        snap = self._snapshot() or {}
        self._queue_view.apply_snapshot(
            queue=snap.get("queue", []),
            turn_active=snap.get("turn_active", False),
            queue_seq=snap.get("queue_seq", 0),
        )
        for item in self._queue_view.queue():
            msg_id = item.get("msg_id")
            if msg_id:
                self._sent_queue.show_item(msg_id, str(item.get("text", "")))
                # BUG under test: the meta-carry line is dropped.

    monkeypatch.setattr(
        app_module.TextualChatApp, "_seed_queue_view", _seed_without_meta_carry,
    )

    read_model = _SnapshotSeededReadModel(
        {
            "msg_id": "m1", "chain_id": "c1", "text": "peer's queued line",
            "meta": {"actor": "alice", "auth_user_id": "alice"},
        }
    )
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(_turn_started(chain_id="c1", seq=2))
        await pilot.pause()

        (entry,) = _flow_user_entries(app)
        assert entry.item.meta.get("actor") is None, (
            "the broken (meta-carry-stripped) seed should reproduce the "
            "misattribution — an empty meta on the promoted entry"
        )
