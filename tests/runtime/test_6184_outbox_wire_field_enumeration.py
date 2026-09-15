"""Tier 1/2: #6184 — OutboxMessage's send-side and receive-side field
enumerations must not drift, and reply_to must never cross the wire.

Real defect (lead-coder, #6184, ``origin/main``): the wire-safe field set
was hand-typed independently at THREE sites — ``agui/protocol.py``'s
``_encode_display`` (3 fields: kind/text/meta), a second, separately
hand-typed streaming-end encode site (the same 3 fields, a second
independent list), and ``OutboxMessage.from_wire``'s own
``object.__setattr__`` body (4 fields: kind/text/meta/reply_to). ``reply_to``
already never crossed the wire (the encode sides omitted it) but nothing
enforced that agreement structurally — a field added to the dataclass could
land in one enumeration and not the others with nothing forcing an error.

Fix: ``OutboxMessage.to_wire_dict()`` (encode) and ``OutboxMessage.from_wire``
(decode) both derive their field set from ``dataclasses.fields()`` — a field
gained later is wire-safe automatically; only a name declared in
``_NON_WIRE_FIELDS`` (currently just ``reply_to``, with its reason recorded
there — a ``TransportRef`` is a process-local runtime routing object, ADR-B:
"do NOT survive crash recovery") is excluded, in ONE place.

Strip-falsified by hand (not committed — a temporarily-added probe field
would itself become a hand-typed entry, the exact anti-pattern #6184's own
accept criterion ④ forbids): with a throwaway dataclass field added and
``to_wire_dict`` reverted to a hardcoded 3-key dict (simulating "encode
side updated, decode side's generic loop was not"), the round trip below
lost the new field's value — confirmed RED — then reverted to the real
generic derivation — confirmed GREEN again.

Real ``OutboxMessage``/``TuiRef``/``encode_frame``/``decode_event``
throughout (CLAUDE.md mock ban) — no curated field-name list anywhere in
this file (accept criterion ④): every assertion below reads the real
dataclass's OWN current field set via ``dataclasses.fields``, never a
list this file re-types.
"""
from __future__ import annotations

import dataclasses

from reyn.interfaces.transport.agui.protocol import decode_event, encode_frame
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.transport import TuiRef


def test_wire_round_trip_preserves_every_non_excluded_field():
    """Tier 2: every dataclass field OutboxMessage currently declares,
    other than reply_to, survives a real encode -> decode round trip
    with its original value intact.

    Reads the field NAMES from ``dataclasses.fields`` (never re-typed
    here) so this test does not itself become a second curated list —
    it asserts the round trip agrees with whatever OutboxMessage's own
    fields are today, not with a name this file wrote down."""
    original = OutboxMessage(kind="agent", text="hello world", meta={"run_id": "r1"})
    decoded = decode_event(*_encode(original))

    for f in dataclasses.fields(OutboxMessage):
        if f.name == "reply_to":
            continue  # covered separately below — excluded by design
        assert getattr(decoded.message, f.name) == getattr(original, f.name), (
            f"field {f.name!r} did not survive the wire round trip"
        )


def test_reply_to_never_crosses_the_wire():
    """Tier 2: reply_to is set to a REAL, non-None TransportRef here — the
    witness that it is dropped by DESIGN (excluded in _NON_WIRE_FIELDS),
    not merely because every other test happens to leave it at its
    default ``None``."""
    original = OutboxMessage(kind="agent", text="hi", meta={}, reply_to=TuiRef())
    ag_type, data = _encode(original)

    assert "reply_to" not in data["_reyn"], (
        "reply_to leaked onto the wire — _NON_WIRE_FIELDS no longer excludes it"
    )
    decoded = decode_event(ag_type, data)
    assert decoded.message.reply_to is None


def test_from_wire_tolerates_an_unrecognised_wire_key():
    """Tier 1: from_wire's generic field loop must not raise on a wire
    dict carrying a key that is not one of OutboxMessage's own fields —
    the real decode call sites pass the WHOLE reyn dict through
    (including its own "frame" tag key), relying on exactly this
    tolerance."""
    msg = OutboxMessage.from_wire(
        frame="display", kind="status", text="t", meta={}, some_future_field="x",
    )
    assert msg.kind == "status"
    assert msg.text == "t"


def _encode(msg: OutboxMessage) -> "tuple[str, dict]":
    ev = encode_frame(DisplayFrame(msg))
    return ev.type, ev.data
