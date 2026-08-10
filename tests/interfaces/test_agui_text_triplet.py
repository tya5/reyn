"""Tier 2: a whole text message rides the canonical AG-UI triplet (ADR-0039 P4).

Canonical AG-UI mandates the text lifecycle ``TEXT_MESSAGE_START`` → one-or-more
``TEXT_MESSAGE_CONTENT`` → ``TEXT_MESSAGE_END``, all correlated by ``messageId``;
a bare ``TEXT_MESSAGE_CONTENT`` is invalid (a strict generic client drops it). P4
synthesizes the triplet for each whole message. This pins both halves of the P4
contract:

- **Generic surface valid**: the wire sequence for a text frame is exactly
  START → CONTENT → END, one shared ``messageId``, CONTENT ``delta`` = full text.
- **reyn invariant preserved (SR6)**: only the CONTENT event carries ``_reyn``
  (START/END decode to ``None``), so the invariant stays 1 frame ⇄ 1
  ``_reyn``-bearing event and the reyn client reconstructs the SAME single frame.

Real instances only — the real codec; no mocks.
"""
from __future__ import annotations

from reyn.interfaces.transport.agui.protocol import (
    TEXT_MESSAGE_CONTENT,
    TEXT_MESSAGE_END,
    TEXT_MESSAGE_START,
    decode_event,
    encode_frame_wire,
)
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


def test_text_frame_expands_to_correlated_triplet() -> None:
    """Tier 2: a text frame's wire sequence is START→CONTENT→END, one messageId,
    CONTENT delta = the whole message text (the canonical lifecycle a generic
    client requires)."""
    start, content, end = encode_frame_wire(
        DisplayFrame(OutboxMessage(kind="agent", text="hello world"))
    )

    assert [start.type, content.type, end.type] == [
        TEXT_MESSAGE_START,
        TEXT_MESSAGE_CONTENT,
        TEXT_MESSAGE_END,
    ]
    mid = content.data.get("messageId")
    assert mid, "the CONTENT event carries a non-empty messageId"
    # START/CONTENT/END all share that one id (the correlation the spec requires).
    assert {start.data.get("messageId"), end.data.get("messageId")} == {mid}
    assert content.data["delta"] == "hello world"


def test_only_content_carries_reyn_start_and_end_decode_to_none() -> None:
    """Tier 2: SR6 — START/END are generic scaffold (no _reyn → decode None); only
    CONTENT reconstructs the reyn frame — the 1 frame ⇄ 1 _reyn-event invariant."""
    seq = encode_frame_wire(DisplayFrame(OutboxMessage(kind="agent", text="hi")))
    start, content, end = seq

    # START and END are standard-only scaffold: the reyn client ignores them.
    assert decode_event(start.type, start.data) is None
    assert decode_event(end.type, end.data) is None

    # Exactly ONE event bears the reconstruction block, and it rebuilds the frame.
    (reyn_bearing,) = [e for e in seq if "_reyn" in e.data]
    assert reyn_bearing is content
    decoded = decode_event(content.type, content.data)
    assert isinstance(decoded, DisplayFrame)
    assert decoded.message.kind == "agent" and decoded.message.text == "hi"


def test_non_text_frame_is_a_single_event() -> None:
    """Tier 2: a non-text frame (e.g. error → RUN_ERROR) is a single wire event,
    not a triplet — the triplet is text-only."""
    (only,) = encode_frame_wire(DisplayFrame(OutboxMessage(kind="error", text="boom")))
    assert only.type != TEXT_MESSAGE_CONTENT
