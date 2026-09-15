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

#6184 BLOCKING round 1 (lead-coder, measured, PR #6187 review) caught two
things a first version of this file got wrong:

1. A hand strip-falsify (edit source, run, confirm RED, edit back, confirm
   GREEN) IS the correct discipline while implementing, but a ONE-TIME
   observation is not itself evidence a future reader can see — it is not
   preserved by committing nothing. Fixed below with a REAL, committed,
   ALWAYS-non-vacuous witness instead (test 4): a local subclass adding
   one extra field. This is NOT the curated field-NAME list accept
   criterion ④ forbids (lead-coder's own clarification: ④ bans hand-typing
   field NAMES, not adding a probe field to a dataclass) — ``to_wire_dict``/
   ``from_wire`` are both generic over ``dataclasses.fields(self/cls)``, so
   the subclass exercises the real mechanism with zero field names typed
   anywhere in this file.
2. Without that subclass witness, test 1 below is vacuous TODAY: the
   current field set (kind/text/meta, reply_to excluded) happens to equal
   exactly the 3 fields the pre-#6184 hand-typed encode dict already had,
   so reverting ``to_wire_dict`` to that hardcoded 3-key form would leave
   test 1 green (CLAUDE.md test review Q4 — "green having run with
   nothing to bite on"). Test 4 is what actually bites regardless of how
   many real fields OutboxMessage currently has.

#6184 BLOCKING round 2 (lead-coder, measured, PR #6187 review): round 1's
own fix over-corrected. ``_dataclass_field_default`` fell back to ``None``
for a field with neither a declared default nor a default_factory — which
made an absent ``"text"`` key produce ``text=None``. ``None`` is not a
graceful degrade for a ``str``-typed field: it relocates a fail-close into
a fail-FAR-AWAY ``TypeError`` at whichever consumer calls a ``str`` method
on the result, instead of avoiding one. The fix distinguishes two cases —
a field WITH a declared default/default_factory uses it (test 5 below,
witnessed via a subclass field with a distinctive non-``""``/non-``{}``
default so a blanket string/dict fallback could not pass it by accident);
a field with NEITHER (today: ``kind``/``text``, the two REQUIRED core
content fields) degrades to ``""`` — the SAME choice ``from_wire``'s own
pre-existing ``kind = str(wire.get("kind") or "")`` line already made for
the sibling no-default field (test 6 below). One rule, not two silently
different ones for two fields in the same position.

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
    fields are today, not with a name this file wrote down. VACUOUS
    against an encode-side regression today (see module docstring, and
    test 4 below, which is not) — kept anyway as the real-usage,
    real-codec-path witness test 4 deliberately does not exercise."""
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


def test_a_field_this_class_gains_later_is_wire_safe_with_no_code_change():
    """Tier 1: THE non-vacuous witness for #6184's own promise (#6184
    BLOCKING round 1 — see module docstring point 2). A local subclass
    adding ONE extra field, never mentioned by name in this file's own
    assertions beyond reading it back off the subclass — to_wire_dict
    and from_wire are both generic over dataclasses.fields(self/cls),
    so a regression reverting either to a hand-typed field list breaks
    THIS test regardless of what OutboxMessage's own real fields are on
    any given day, unlike test 1 above."""
    @dataclasses.dataclass(frozen=True)
    class _OneExtraFieldMessage(OutboxMessage):
        probe: str = "probe-default"

    original = _OneExtraFieldMessage(kind="status", text="t", meta={})
    wire = original.to_wire_dict()
    assert "probe" in wire, (
        "to_wire_dict did not include a field gained by a subclass — "
        "it has reverted to a hand-typed field list"
    )

    rebuilt = _OneExtraFieldMessage.from_wire(**wire)
    assert rebuilt.probe == original.probe


def test_from_wire_missing_key_uses_the_fields_own_declared_default():
    """Tier 1: #6184 BLOCKING round 1 (lead-coder, measured) — a field
    genuinely ABSENT from the wire dict, and that DECLARES a default or
    default_factory, falls back to THAT field's own value — never a
    hand-typed "" applied regardless of type. Witnessed via a subclass
    field whose default is neither "" nor {} (a blanket string/dict
    fallback would not reproduce this exact value, so this cannot pass
    by accident the way a field genuinely defaulting to "" or {} could)."""
    @dataclasses.dataclass(frozen=True)
    class _ProbeDefaultMessage(OutboxMessage):
        probe: str = "distinct-default-xyz"

    msg = _ProbeDefaultMessage.from_wire(kind="status")  # "probe" key entirely absent
    assert msg.probe == "distinct-default-xyz"


def test_from_wire_missing_key_for_a_no_default_field_degrades_to_empty_string_not_none():
    """Tier 1: #6184 BLOCKING round 2 (lead-coder, measured) — a field
    with NEITHER a declared default NOR a default_factory (``text``,
    one of ``from_wire``'s two REQUIRED core content fields) must
    degrade to ``""`` when its wire key is absent, the SAME choice
    ``from_wire``'s own pre-existing ``kind = str(wire.get("kind") or
    "")`` line already makes for the OTHER no-default field — one rule,
    not two. A first version of this test asserted ``None`` here; that
    was WRONG (lead-coder, PR #6187 review round 2) — ``None`` is not a
    graceful degrade for a ``str``-typed field, it only relocates a
    fail-close into a far-away ``TypeError`` at whichever consumer
    calls a ``str`` method on the result."""
    msg = OutboxMessage.from_wire(kind="status")  # "text" key entirely absent
    assert msg.text == ""


def _encode(msg: OutboxMessage) -> "tuple[str, dict]":
    ev = encode_frame(DisplayFrame(msg))
    return ev.type, ev.data
