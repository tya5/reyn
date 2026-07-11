"""Tier 2: present render-nodes ride the wire inert + edge re-guard (P2, G2/A5).

A ``present`` op's render model is a ``list[dict]`` neutralized at construction
(inert-on-wire). P2 carries it as a reyn Custom event (present-on-wire, G2) and
adds a per-connection re-guard at the transport edge (A5) for a heterogeneous
client whose upstream may not have neutralized (or neutralized for a different
surface). This pins both:

- a ``presentation`` frame round-trips over the wire as a DisplayFrame with its
  nodes intact (rides Custom, decoded back to the same kind); and
- a leaf carrying an ESC / control sequence is neutralized at the edge — the
  decoded node no longer contains the raw control bytes (inert by construction,
  re-asserted per connection).

Real instances only — the real codec + AgUiTransport + the real presentation
guard neutralizer; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.protocol import encode_frame, to_sse
from reyn.interfaces.transport.agui.state import reguard_nodes
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

# An OSC-52 clipboard escape (a real terminal attack surface) embedded in a leaf.
_ESC_LEAF = "safe\x1b]52;;ZXZpbA==\x07tail"


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


def test_reguard_neutralizes_control_sequences_in_leaves() -> None:
    """Tier 2: the edge re-guard strips ESC/control bytes from every node leaf,
    preserving structure (A5 defense-in-depth for heterogeneous clients)."""
    nodes = [{"type": "text", "text": _ESC_LEAF}, {"type": "table", "rows": [[_ESC_LEAF]]}]
    guarded = reguard_nodes(nodes)

    # Structure preserved; ESC byte removed from every leaf.
    assert "\x1b" not in guarded[0]["text"]
    assert "\x1b" not in guarded[1]["rows"][0][0]
    # The non-control content survives (neutralize strips control, not text).
    assert "safe" in guarded[0]["text"] and "tail" in guarded[0]["text"]


@pytest.mark.asyncio
async def test_presentation_frame_rides_wire_inert() -> None:
    """Tier 2: a presentation frame round-trips as a DisplayFrame(kind=presentation)
    with nodes re-guarded at the transport edge (no raw control bytes)."""
    frame = DisplayFrame(
        OutboxMessage(kind="presentation", text="", meta={"nodes": [{"type": "text", "text": _ESC_LEAF}]})
    )
    sse = to_sse(encode_frame(frame)) + to_sse(
        encode_frame(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    )

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    decoded = []
    async for f in transport.frames():
        decoded.append(f)

    pres = decoded[0]
    assert isinstance(pres, DisplayFrame) and pres.message.kind == "presentation"
    # present-on-wire is inert: the ESC byte is gone after the edge re-guard.
    leaf = pres.message.meta["nodes"][0]["text"]
    assert "\x1b" not in leaf and "safe" in leaf
