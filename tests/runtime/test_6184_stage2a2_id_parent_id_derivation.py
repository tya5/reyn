"""Tier 1/2: #6184 段2a-2 — OutboxMessage.__post_init__ derives id/parent_id
from kind/meta, zero producer diff.

Design/ruling: lead-coder, issuecomment-5682240477 (adopting the
architect's #4691-consumer-faithful 4-branch rule, with one owner-visible
correction: `call_id` is a THIRD PARTY's identifier — litellm's own
response id, `llm.py:_response_call_id`'s own docstring — not something
reyn's own "unique per issuance" contract for `id` can claim
independently for the `call:`-prefixed branch).

#6184 BLOCKING precedent (this session, #6187/#6190 reviews): a
"NOTHING reads this yet" claim needs a committed witness disclosing that
it is a MERGE-TIME fact, not a standing invariant — applied proactively
here too (see the bottom of this docstring) rather than waiting for a
repeat finding.

Real ``OutboxMessage`` throughout (CLAUDE.md mock ban) — no curated
field-name list; every field-set assertion reads ``dataclasses.fields``
(matching this arc's own established discipline in
``test_6184_outbox_wire_field_enumeration.py``).

#6184 accept criterion 4 (unchanged since 段2a-1, re-disclosed here for
the SAME reason lead-coder's #6190 review required for the id/parent_id
fields specifically): "0 readers of id/parent_id in src/" remains a
MERGE-TIME OBSERVATION, not a permanent invariant — 段2b's whole point
is to wire a consumer up to read these (the SAME derivation this file
tests), at which point the claim goes stale ON PURPOSE. No standing gate
enforces it; read ``git log`` for ``outbox.py``'s own ``id``/
``parent_id`` fields to see whether it still holds.
"""
from __future__ import annotations

from reyn.runtime.outbox import OutboxMessage


def test_producer_side_diff_is_zero_existing_construction_still_works():
    """Tier 1: #6184 段2a-2 accept criterion ⑴ — a construction call site
    written before this stage (no id/parent_id keyword at all, the SAME
    shape every real producer in src/ still uses) builds without error
    and gets a REAL derived value, not a leftover None from a forgotten
    wiring."""
    msg = OutboxMessage(kind="agent", text="hi", meta={"call_id": "abc123"})
    assert msg.id == "call:abc123"


def test_turn_owning_row_does_not_parent_itself():
    """Tier 1: #6184 段2a-2 accept criterion ⑵ — the turn's own parent
    row (kind="user") owns an id but declares NO parent_id of its own;
    a row cannot be its own child."""
    turn_row = OutboxMessage(kind="user", text="hello", meta={"chain_id": "t1"})
    assert turn_row.id == "turn:t1"
    assert turn_row.parent_id is None


def test_call_owning_agent_row_nests_under_its_own_turn():
    """Tier 1: the sibling half of the branch table — a call-owning
    agent row (has its own call_id) still declares parent_id pointing
    at the SAME turn key the turn-owning row above asserts as its id —
    the two rows are meant to link."""
    agent_row = OutboxMessage(
        kind="agent", text="reply", meta={"call_id": "abc123", "chain_id": "t1"},
    )
    assert agent_row.id == "call:abc123"
    assert agent_row.parent_id == "turn:t1"


def test_agent_row_without_call_id_declares_no_id_never_an_empty_string():
    """Tier 1: #6184 dispatch ③ — the SAME judgment
    _register_call_parent's own ``if kind != "agent" or not call_id:
    return`` already makes: an agent row with no call_id owns no
    call-level group. Witnesses that this is None, never a fabricated
    "" (the shared-empty-key hazard _response_call_id's own docstring
    warns against)."""
    msg = OutboxMessage(kind="agent", text="hi", meta={"chain_id": "t1"})
    assert msg.id is None
    assert msg.parent_id == "turn:t1"


def test_no_chain_id_at_all_produces_no_fabricated_turn_key():
    """Tier 1: the same no-key-when-absent treatment applies to
    chain_id as to call_id — a row with neither key declares neither
    id nor parent_id, never a shared fallback."""
    msg = OutboxMessage(kind="status", text="hi", meta={})
    assert msg.id is None
    assert msg.parent_id is None


def test_call_and_turn_namespaces_cannot_collide_by_construction():
    """Tier 1: #6184 accept criterion ⑸ — "no two entries assert the
    same id" via the table's own exclusivity, witnessed structurally:
    the identical raw string used as BOTH a call_id and a chain_id
    produces two DIFFERENT ids (the `call:`/`turn:` prefix separation
    is the mechanism, not merely today's inputs happening to differ)."""
    same_raw_value = "shared-raw-string"
    call_owning = OutboxMessage(kind="agent", text="a", meta={"call_id": same_raw_value})
    turn_owning = OutboxMessage(kind="user", text="u", meta={"chain_id": same_raw_value})
    assert call_owning.id != turn_owning.id
    assert call_owning.id == f"call:{same_raw_value}"
    assert turn_owning.id == f"turn:{same_raw_value}"


def test_two_distinct_call_ids_never_produce_the_same_id():
    """Tier 1: ordinary exclusivity within ONE namespace — two agent
    rows for genuinely different calls get different ids."""
    first = OutboxMessage(kind="agent", text="a", meta={"call_id": "call-1"})
    second = OutboxMessage(kind="agent", text="b", meta={"call_id": "call-2"})
    assert first.id != second.id


def test_from_wire_does_not_rederive_passes_through_whatever_the_wire_carried():
    """Tier 1: #6184 段2a-2 accept criterion ⑶ — from_wire must NOT
    re-run the derivation; a wire-decoded id/parent_id is whatever the
    ORIGIN process already derived, unchanged here. Constructed with
    values the derivation rule above would NEVER produce on its own
    (mismatched meta) specifically so a silent re-derivation would be
    caught by this test overwriting them."""
    msg = OutboxMessage.from_wire(
        kind="agent", text="t", meta={"call_id": "would-derive-differently"},
        id="wire-origin-id", parent_id="wire-origin-parent",
    )
    assert msg.id == "wire-origin-id"
    assert msg.parent_id == "wire-origin-parent"
