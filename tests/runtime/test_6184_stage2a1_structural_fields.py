"""Tier 1/2: #6184 段2a-1 — OutboxMessage gains 5 structural fields
(id/parent_id/operation/subject/details), purely additively.

Design: architect, issuecomment-5681485023. Ruling: lead-coder,
issuecomment-5681510010. This stage's own definition of done: every new
field defaults (no existing construction call site changes), NOTHING in
``src/`` reads any of them yet (confirmed by census — no ``.id``/
``.parent_id``/``.operation``/``.subject``/``.details`` access on an
``OutboxMessage`` anywhere outside ``outbox.py``'s own definition), and
all 5 survive a real wire round trip via #6187's own field-derivation
machinery (``to_wire_dict``/``from_wire``) — confirmed here, not assumed
(dispatch, verbatim: "#6187 の derive で自動のはず。確かめてください、
前提にしない").

#6184 BLOCKING (lead-coder, PR #6190 review): "NOTHING reads them yet"
is a MERGE-TIME OBSERVATION, not a standing invariant this test file
enforces going forward — unlike #6187's own field-derivation contract
(which genuinely IS permanent), 段2b's whole point is to wire a
consumer up to read these fields, at which point the census above
becomes stale ON PURPOSE. No permanent gate is added here for that
reason (a `tests/scaffold/` `triggered_by`/`removed_by` probe was
considered and rejected — lead-coder's own reasoning: it would create a
NEW failure mode, "段2b の PR がその削除を忘れる", worse than the gap
it would close). This module's own "0 readers" claim is true as of
merge; read `git log` for `outbox.py`'s ``id``/``parent_id``/
``operation``/``subject``/``details`` fields to see whether it still
is.

``operation``'s closed 2-value vocabulary (``NEW``/``UPDATE``) is
deliberately narrower than the 4 landing shapes #6184's own census found
(NEST/FLAT-bypass/UPDATE-in-place/DROP) — see :class:`Operation`'s own
docstring for why a producer cannot assert NEST/FLAT (that is the
renderer's decision) or DROP (that is a consumer's dedup judgment, never
a producer's own declaration about itself).

Real ``OutboxMessage``/``Operation``/``encode_frame``/``decode_event``
throughout (CLAUDE.md mock ban).
"""
from __future__ import annotations

import dataclasses

from reyn.interfaces.transport.agui.protocol import decode_event, encode_frame
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import Operation, OutboxMessage


def test_all_five_structural_fields_default_so_existing_construction_is_unaffected():
    """Tier 1: the #6184 段2a-1 accept criterion, directly — a construction
    call site written before this stage (no keyword for any of the 5 new
    fields) still builds, and each new field reads back at its own
    documented safe default."""
    msg = OutboxMessage(kind="agent", text="hello", meta={})
    assert msg.id is None
    assert msg.parent_id is None
    assert msg.operation is Operation.NEW
    assert msg.subject is None
    assert msg.details == {}


def test_all_five_structural_fields_survive_a_real_wire_round_trip():
    """Tier 2: #6184's own dispatch — confirmed, not assumed, that
    #6187's field-derivation machinery covers a REAL new field, not just
    the 4 that existed when it landed. Reads the field NAMES from
    dataclasses.fields (never re-typed here), matching this arc's own
    established discipline in test_6184_outbox_wire_field_enumeration.py."""
    original = OutboxMessage(
        kind="agent", text="hi", meta={"run_id": "r1"},
        id="issuance-1", parent_id="parent-1", operation=Operation.UPDATE,
        subject="subject text", details={"argv": ["ls", "-la"]},
    )
    ev = encode_frame(DisplayFrame(original))
    decoded = decode_event(ev.type, ev.data)

    for f in dataclasses.fields(OutboxMessage):
        if f.name == "reply_to":
            continue  # excluded by design — covered elsewhere (#6184 original PR)
        assert getattr(decoded.message, f.name) == getattr(original, f.name), (
            f"field {f.name!r} did not survive the wire round trip"
        )


def test_operation_vocabulary_has_no_nest_flat_or_drop_member():
    """Tier 1: structural witness for the design's own central claim
    (architect, issuecomment-5681485023) — a producer CANNOT declare
    NEST/FLAT (the renderer's decision, driven by whether parent_id is
    set) or DROP (a consumer's dedup judgment) through this vocabulary,
    because those values are not members of it at all."""
    values = {member.value for member in Operation}
    assert values == {"new", "update"}


def test_operation_update_can_carry_a_new_kind_value():
    """Tier 1: #6184's own census finding (C7,
    app.py:_handle_intervention_answer_event) — the ONE landing shape
    that rewrites kind itself on an already-emitted entry. operation
    is a producer-side declaration, not itself a kind-carrier, but this
    witnesses that UPDATE's own contract (a full new value, never a
    diff — see Operation.UPDATE's own docstring) is not violated by
    combining it with a kind change on the SAME OutboxMessage."""
    updated = OutboxMessage(
        kind="intervention_resolved", text="", meta={"intervention_id": "iv-1"},
        id="entry-1", operation=Operation.UPDATE,
    )
    assert updated.kind == "intervention_resolved"
    assert updated.operation is Operation.UPDATE


def test_from_wire_degrades_an_unrecognised_operation_value_never_raises():
    """Tier 1: the SAME degrade-never-raise shape
    _normalize_spillability/_normalize_history_entry_kind already use
    (chat_message.py), not an invented one (dispatch, verbatim: "既存
    pattern に倣ってください。独自の扱いを発明しないこと"). from_wire
    bypasses __post_init__ entirely, so this exercises the SEPARATE
    normalization from_wire's own docstring says it must do itself."""
    msg = OutboxMessage.from_wire(kind="status", text="t", operation="not-a-real-value")
    assert msg.operation is Operation.NEW  # Operation.default(), never a raise


def test_construction_side_also_degrades_an_unrecognised_operation_value():
    """Tier 1: sibling of the from_wire test above — __post_init__'s own
    normalization call (the in-process construction path) degrades the
    same way, not just the wire-decode path. A plain (non-Operation,
    non-recognised-string) value can reach here from any caller that
    has not yet been updated to pass a real Operation member."""
    msg = OutboxMessage(kind="status", text="t", meta={}, operation="not-a-real-value")
    assert msg.operation is Operation.NEW
