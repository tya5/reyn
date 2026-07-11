"""Tier 2: OutboxMessage.kind is a closed vocabulary, validated at construction
but lenient on the untrusted wire (ADR-0039 P6b).

Two directions plus a dead-entry catch:

- **Production-side gate:** ``OutboxMessage(kind=<not in VOCABULARY>)`` raises at
  construction (fail-visible — catches the dynamic/helper constructions a static
  scan misses). Strip the ``__post_init__`` check → an un-vocabularied producer
  slips through → the disposition gate can no longer guarantee no wire leak.
- **Wire-side leniency:** ``OutboxMessage.from_wire(kind=<unknown>)`` does NOT
  raise — the AG-UI decode path rebuilds frames from an untrusted remote peer and
  must ignore-unknown / graceful-degrade, never fail-close.
- **No dead vocabulary entry:** every VOCABULARY kind is referenced in ``src`` (a
  producer construction, a renderer branch, a codec map, or a profile entry).

Real instances only — the real dataclass, the real decode path; no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.transport.agui.protocol import decode_event, encode_frame
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import (
    CONTROL_KINDS,
    DISPLAY_KINDS,
    VOCABULARY,
    OutboxMessage,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"


def test_construction_accepts_every_vocabulary_kind() -> None:
    """Tier 2: each of the closed-vocabulary kinds constructs (validated __init__)."""
    for kind in VOCABULARY:
        msg = OutboxMessage(kind=kind, text="x")
        assert msg.kind == kind
    # The vocabulary is the disjoint union of DISPLAY_KINDS and CONTROL_KINDS.
    assert DISPLAY_KINDS.isdisjoint(CONTROL_KINDS)
    assert VOCABULARY == DISPLAY_KINDS | CONTROL_KINDS


def test_construction_rejects_an_unvocabularied_kind() -> None:
    """Tier 2: a producer constructing an un-vocabularied kind (strip target)
    raises at construction. Strip the ``__post_init__`` gate → no raise → RED."""
    with pytest.raises(ValueError):
        OutboxMessage(kind="not_a_real_kind", text="x")
    # A near-miss (control-sentinel-shaped but unknown) is still rejected.
    with pytest.raises(ValueError):
        OutboxMessage(kind="__not_a_sentinel__", text="x")


def test_from_wire_is_lenient_on_an_unknown_kind() -> None:
    """Tier 2: the untrusted wire path tolerates an unknown kind (strip target)
    (ignore-unknown), NEVER fail-close. Route from_wire through __post_init__ →
    this RAISES → RED (the graceful-degrade contract broken)."""
    msg = OutboxMessage.from_wire(kind="some_future_kind", text="body", meta={"k": "v"})
    assert msg.kind == "some_future_kind"
    assert msg.text == "body"
    assert msg.meta == {"k": "v"}
    assert isinstance(msg, OutboxMessage)


def test_decode_of_an_unknown_wire_kind_is_graceful_not_a_raise() -> None:
    """Tier 2: an unknown display kind arriving over the AG-UI wire decodes to a
    DisplayFrame (graceful), not an exception — the end-to-end leniency contract.

    Build a real wire event whose ``_reyn`` block carries an unknown kind (via
    from_wire so encode itself doesn't fail-close), then decode it."""
    wire = encode_frame(
        DisplayFrame(OutboxMessage.from_wire(kind="a_kind_this_client_never_heard_of", text="hi"))
    )
    decoded = decode_event(wire.type, wire.data)  # must NOT raise
    assert isinstance(decoded, DisplayFrame)
    assert decoded.message.kind == "a_kind_this_client_never_heard_of"
    assert decoded.message.text == "hi"


def test_no_dead_vocabulary_entry() -> None:
    """Tier 2: every VOCABULARY kind is referenced (reverse dead-entry catch) as a
    ``"kind"`` string literal somewhere in ``src`` — a producer construction, a
    renderer branch, a codec map, or a profile entry. A vocabulary member with no
    live reference is dead ⇒ RED (remove it)."""
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in _SRC.rglob("*.py")
        if "__pycache__" not in p.parts
    )
    dead = {kind for kind in VOCABULARY if f'"{kind}"' not in blob}
    assert not dead, (
        f"vocabulary kinds with no live reference in src (dead entries): {sorted(dead)}"
    )
