"""Tier 2: a generic AG-UI client degrades gracefully on reyn Custom (P2, D6).

AG-UI has no normative "ignore-unknown" clause, so reyn owns it: a foreign or
unknown event (no reyn ``_reyn`` reconstruction block, or a reyn Custom a generic
client does not understand) is SKIPPED, not fatal. This pins the decoder side of
that contract:

- an event with no ``_reyn`` block decodes to ``None`` (skipped); and
- interleaving such foreign events in the SSE stream does not disturb the reyn
  frames — the real frames still arrive in order, the unknown ones vanish.

Real instances only — the real codec + AgUiTransport; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.protocol import decode_event, encode_frame, to_sse
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


def test_foreign_event_decodes_to_none() -> None:
    """Tier 2: an event with no reyn reconstruction block is ignored (None)."""
    # A standard AG-UI event a generic server might send that reyn did not encode.
    assert decode_event("TEXT_MESSAGE_CONTENT", {"role": "assistant", "delta": "hi"}) is None
    # A reyn-shaped Custom with a name but no _reyn block is likewise skipped.
    assert decode_event("CUSTOM", {"name": "some.future.reyn.event", "value": {}}) is None


@pytest.mark.asyncio
async def test_foreign_events_interleaved_are_skipped() -> None:
    """Tier 2: foreign events interleaved with reyn frames vanish; the reyn
    frames still arrive in order (ignore-unknown, not fatal)."""
    foreign = "event: CUSTOM\ndata: {\"name\": \"vendor.x\", \"value\": 1}\n\n"
    sse = (
        foreign
        + to_sse(encode_frame(DisplayFrame(OutboxMessage(kind="agent", text="one"))))
        + foreign
        + to_sse(encode_frame(DisplayFrame(OutboxMessage(kind="agent", text="two"))))
        + to_sse(encode_frame(DisplayFrame(OutboxMessage(kind="__end__", text=""))))
    )

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    texts = []
    async for f in transport.frames():
        if isinstance(f, DisplayFrame) and f.message.kind == "agent":
            texts.append(f.message.text)

    assert texts == ["one", "two"]
