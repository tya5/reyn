"""Tier 1: #5989 ④ (architect design, lead-coder ruling) — the recovery
half of ③'s own observability. Subject: a queued sent-queue item does NOT
get permanently stuck, never "a WARNING is logged".

## Root cause (architect, verified against ``state.py`` directly)

``RemoteQueueView._last_seq`` has 4 writers; only ``apply_snapshot``
ASSIGNS it (``self._last_seq = queue_seq``, unconditional). A
``turn_started`` for an item generated BEFORE that snapshot but delivered
to the client AFTER it reads ``seq <= self._last_seq`` and is rejected by
the seq-gate. ``turn_started`` is a once-per-turn edge the server never
resends, and ``inbox_cancel`` only fires when the operator cancels — so a
rejected delta of either kind has no OTHER chance to ever arrive. Before
this fix, the item it would have promoted/removed stayed in
:attr:`RemoteQueueView.items` (and, one layer up, in the TUI's own
sent-queue widget) forever — the exact "stuck" shape.

## Fix (same shape ``apply_user_submitted``'s own ``is_own_pending``
already uses)

``stuck`` (the matching item is still present) is itself the proof this
delta was never actually applied — an already-applied item would already
be gone — so applying it here can never create a double-promote/double-
remove. The delta is applied (the item removed), but :attr:`_last_seq` is
NOT regressed backward, so the ordering guarantee every OTHER item's own
gate relies on is untouched.

Real ``RemoteQueueView`` throughout — no mocks, this class has no
collaborator to fake. Each reproduction is the MINIMAL shape architect's
own root-cause account names: seed a snapshot whose ``queue_seq`` already
supersedes an item's own dispatch/cancel ``seq`` (an out-of-order delivery,
not a hand-picked coincidence — see each test's own setup for why this is
the realistic case, not a contrived one).
"""
from __future__ import annotations

from reyn.interfaces.transport.agui.state import RemoteQueueView


def test_a_turn_started_generated_before_a_snapshot_still_promotes_its_item() -> None:
    """Tier 1: the exact repro architect's own root-cause account gives —
    a snapshot's ``queue_seq`` (5) already supersedes a ``turn_started``
    (seq=3) generated for an item BEFORE that snapshot was taken. Before
    ④, this delta was rejected and the item stayed in ``items`` forever
    (``turn_started`` never resends). After ④: the item is promoted
    (removed) anyway.

    Strip-falsify (verified by hand, file-internal Edit only, reverted):
    reverting ``apply_turn_started``'s ``stuck`` exception (the
    ``if seq <= self._last_seq: ... return False`` shape, no application)
    makes this test fail — ``m1`` stays in ``items`` and ``applied`` reads
    ``False``."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )

    applied = view.apply_turn_started(chain_id="c1", seq=3)

    assert applied is True, "the stuck item's own turn_started must be applied, not dropped"
    assert "m1" not in view.items, "the item must not stay queued forever"


def test_an_inbox_cancel_generated_before_a_snapshot_still_removes_its_item() -> None:
    """Tier 1: the same shape as the turn_started test above, through
    ``apply_inbox_cancel`` — an operator's own cancel, generated before a
    snapshot that already supersedes it, must not leave the row sitting
    in the queue looking un-cancelled."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )

    applied = view.apply_inbox_cancel(msg_id="m1", seq=3)

    assert applied is True, "the stuck item's own cancel must be applied, not dropped"
    assert "m1" not in view.items, "a cancelled item must not stay queued forever"


def test_the_seq_gate_still_does_not_regress_for_other_items() -> None:
    """Tier 1: deny side (architect's own safety claim, falsified directly)
    — applying a stuck item's delta must NOT regress ``_last_seq``
    backward. A genuinely stale delta for a DIFFERENT item, arriving
    after the stuck-item recovery above, must still be rejected exactly
    as before — the ordering guarantee the class's own seq-gate exists
    for is untouched by the new exception."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )
    view.apply_turn_started(chain_id="c1", seq=3)  # the stuck-item recovery above
    assert view.baseline_seq() == 5, "arrange: the recovery must not have moved the baseline"

    # A genuinely stale delta for a DIFFERENT (never-queued) item, seq=4
    # (still <= the baseline) — must still be rejected, same as always.
    stale = view.apply_turn_started(chain_id="c-unrelated", seq=4)

    assert stale is False, "a real stale delta for an unrelated item must still be rejected"


def test_a_genuine_replay_after_recovery_is_still_a_quiet_no_op() -> None:
    """Tier 1: deny side — once a stuck item is recovered, a REPLAY of the
    SAME turn_started delta (the server occasionally redelivers over a
    reconnect) must be a harmless no-op, not a second removal attempt
    (there is nothing left to remove) and not a second WARNING (the item
    is legitimately gone now — ``stuck`` correctly reads empty)."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )
    view.apply_turn_started(chain_id="c1", seq=3)  # recovers m1
    assert "m1" not in view.items, "arrange: m1 must already be gone"

    replay = view.apply_turn_started(chain_id="c1", seq=3)  # same delta again

    assert replay is False, "a replay of an already-recovered delta is a genuine no-op"
