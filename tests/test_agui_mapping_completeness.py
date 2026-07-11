"""Tier 2: every renderer Frame kind has an AG-UI encode→decode mapping (P2).

The wire version of P1's dual-stream completeness gate. The Frame vocabulary the
renderer consumes has two halves:

- **DisplayFrame kinds** — the ``OutboxMessage.kind`` literals the renderer's
  ``message`` / ``format_inline_message`` dispatch on (AST-scanned from the
  renderer source, NOT from the codec's own table — non-circular).
- **EventFrame types** — the eight ``renderer_chat_events()`` the transport
  forwards (derived, not hand-listed).

For EACH, the codec must round-trip it: ``encode_frame`` → SSE → ``decode_event``
must reconstruct the SAME kind/type and payload. A kind the codec drops or
mangles ⇒ the round-trip assertion fails ⇒ RED — so a new renderer kind that the
wire cannot carry fails CI instead of silently vanishing on the wire.

The enumeration reads the renderer's real code + the derived event set (two
independent sources), never the codec's mapping tables, so the gate is not
circular.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.protocol import (
    decode_event,
    encode_frame,
    parse_sse_blocks,
    to_sse,
)
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    renderer_chat_events,
)
from reyn.runtime.outbox import OutboxMessage

_RENDERER = (
    Path(__file__).resolve().parents[1]
    / "src" / "reyn" / "interfaces" / "repl" / "renderer.py"
)

# The renderer functions that dispatch on ``OutboxMessage.kind``. Scanning these
# (not the whole file) keeps the vocabulary to actual display-kind branches.
_DISPLAY_DISPATCH_FUNCS = {"message", "format_inline_message"}


def _renderer_display_kinds() -> set[str]:
    """Every ``kind`` string literal the renderer's display-dispatch functions
    compare against — the DisplayFrame vocabulary, read from renderer source."""
    tree = ast.parse(_RENDERER.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in _DISPLAY_DISPATCH_FUNCS):
            continue
        for cmp_node in ast.walk(node):
            if not isinstance(cmp_node, ast.Compare):
                continue
            for lit in ast.walk(cmp_node):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    kinds.add(lit.value)
    return kinds


def _roundtrip(frame):
    """encode → SSE → parse → decode, exactly as the wire does."""
    sse = to_sse(encode_frame(frame))
    (ev,) = parse_sse_blocks(sse.split("\n"))
    return decode_event(ev.type, ev.data)


def test_every_display_kind_round_trips_over_the_wire() -> None:
    """Tier 2: each renderer display kind encodes→decodes back to the same kind
    and text/meta. Unmapped / lossy ⇒ RED (the wire-drop bug, designed out)."""
    kinds = _renderer_display_kinds()

    # Sanity: the scan actually found the renderer's vocabulary (a broken scan
    # that found nothing must not vacuously pass).
    assert {"agent", "error", "presentation", "intervention"} <= kinds

    for kind in kinds:
        frame = DisplayFrame(OutboxMessage(kind=kind, text="body", meta={"k": "v"}))
        decoded = _roundtrip(frame)
        assert isinstance(decoded, DisplayFrame), f"{kind!r} did not decode to a DisplayFrame"
        assert decoded.message.kind == kind, f"{kind!r} kind mangled → {decoded.message.kind!r}"
        assert decoded.message.text == "body"
        assert decoded.message.meta == {"k": "v"}


def test_every_forwarded_chat_event_round_trips_over_the_wire() -> None:
    """Tier 2: each of the eight renderer_chat_events encodes→decodes back to the
    same event type and data. Unmapped / lossy ⇒ RED."""
    events = renderer_chat_events()

    # Sanity: the derived set is the expected non-trivial vocabulary.
    assert {"tool_called", "tool_returned", "tool_failed", "turn_started"} <= events

    for etype in events:
        frame = EventFrame(Event(type=etype, data={"tool": "grep_files"}))
        decoded = _roundtrip(frame)
        assert isinstance(decoded, EventFrame), f"{etype!r} did not decode to an EventFrame"
        assert decoded.event.type == etype, f"{etype!r} type mangled → {decoded.event.type!r}"
        assert decoded.event.data == {"tool": "grep_files"}
