"""Tier 1: #6213 — `outbox.py`'s `_derive_id_and_parent_id` gains a
`dispatch_id`-driven branch, checked BEFORE the pre-existing 4-branch
table (`call_id`/`chain_id`-derived `call:`/`turn:` ids).

Covers the `outbox.py`-level half of the issue's own accept criteria:
- a `tool_call_started` row's `id` becomes `tool:{dispatch_id}`, and its
  `parent_id` is UNCHANGED (still `call:{call_id}` when `call_id` is
  present — #6213's own ruling: "parent_id は不変").
- a `tool_call_completed`/`failed` row's `parent_id` becomes
  `tool:{dispatch_id}` INSTEAD OF `call:{call_id}` — #6213's own accept
  ⑺: this is correct AS NESTING (the row now belongs to the started row
  it settles, not the whole litellm call), but must not change what an
  ORPHANED completion's own PRESENTATION looks like (pinned at the
  app.py layer, `tests/interfaces/test_6213_running_tools_correlation.py`).
- a message with NO `dispatch_id` falls through to the pre-#6213 rule,
  byte-identical (regression guard: this fix is additive).
- `from_wire` does NOT re-derive `id`/`parent_id` from `dispatch_id` —
  the wire-carried value (whatever the ORIGIN process already derived)
  passes through verbatim, even if `meta["dispatch_id"]` in the wire
  payload is tampered with — #6213's own explicit "confirm not
  re-derived on remote" requirement.
"""
from __future__ import annotations

import json

from reyn.interfaces.transport.agui.protocol import decode_event, encode_frame
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage, _derive_id_and_parent_id


def test_tool_call_started_id_is_tool_prefixed_dispatch_id():
    """Tier 1: a started row's own `id` becomes `tool:{dispatch_id}`."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_started", meta={"dispatch_id": "abc123"},
    )
    assert id_ == "tool:abc123"
    assert parent_id is None  # no call_id in this meta


def test_tool_call_started_parent_id_is_unchanged_by_dispatch_id():
    """Tier 1: LOAD-BEARING — a started row's `parent_id` (its own litellm
    CALL group) is UNCHANGED by `dispatch_id`'s presence — still
    `call:{call_id}` (#6213's own ruling: "parent_id は不変"). Only `id`
    is new."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_started",
        meta={"dispatch_id": "abc123", "call_id": "resp-1"},
    )
    assert id_ == "tool:abc123"
    assert parent_id == "call:resp-1"


def test_tool_call_completed_parent_id_is_tool_prefixed_dispatch_id():
    """Tier 1: a completed row's `parent_id` becomes `tool:{dispatch_id}`
    -- NOT `call:{call_id}`, even when `call_id` IS present (#6213's own
    nesting correction: the completion belongs to the started row it
    settles, not the whole call)."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_completed",
        meta={"dispatch_id": "abc123", "call_id": "resp-1"},
    )
    assert id_ is None
    assert parent_id == "tool:abc123"


def test_tool_call_failed_parent_id_is_tool_prefixed_dispatch_id():
    """Tier 1: the failed-kind sibling of the completed test above."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_failed", meta={"dispatch_id": "xyz789"},
    )
    assert id_ is None
    assert parent_id == "tool:xyz789"


def test_no_dispatch_id_falls_through_to_the_pre_6213_rule_unchanged():
    """Tier 1: regression guard — a message with NO `dispatch_id` (any
    producer that predates this fix) gets the EXACT pre-#6213 derivation
    (byte-identical to the original 4-branch table) — this fix is
    additive, never a replacement."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_started", meta={"call_id": "resp-1"},
    )
    assert (id_, parent_id) == (None, "call:resp-1")

    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_completed", meta={"call_id": "resp-1"},
    )
    assert (id_, parent_id) == (None, "call:resp-1")

    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_completed", meta={},
    )
    assert (id_, parent_id) == (None, None)


def test_from_wire_does_not_rederive_id_from_a_tampered_meta_dispatch_id():
    """Tier 1: LOAD-BEARING — `from_wire` bypasses `_derive_id_and_
    parent_id` entirely (#6184's own established contract): the
    wire-carried `id` (whatever the ORIGIN process already derived) is
    used VERBATIM, even when the wire payload's own `meta["dispatch_id"]`
    is tampered with AFTER encoding — a re-deriving implementation would
    compute a DIFFERENT id here and fail this test.

    strip: make `from_wire` call `_derive_id_and_parent_id` (or read
    `meta["dispatch_id"]` again) -- this test would then see the
    TAMPERED value reflected in `wired_msg.id`, not the original."""
    original = OutboxMessage(
        kind="tool_call_started", text="grep",
        meta={"tool": "grep", "dispatch_id": "abc123"},
    )
    assert original.id == "tool:abc123", "setup: confirm the origin process derived the id we expect"

    ev = encode_frame(DisplayFrame(original))
    wire_data = json.loads(json.dumps(ev.data))
    wire_data["_reyn"]["meta"]["dispatch_id"] = "TAMPERED"
    decoded = decode_event(ev.type, wire_data)
    wired_msg = decoded.message

    assert wired_msg.id == "tool:abc123", (
        f"from_wire must use the wire-carried id verbatim, not re-derive from "
        f"meta -- got {wired_msg.id!r}"
    )
