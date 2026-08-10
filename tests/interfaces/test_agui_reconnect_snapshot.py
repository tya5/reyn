"""Tier 2: on connect the server emits MESSAGES_SNAPSHOT + STATE_SNAPSHOT (P2, A4).

The reconnect contract (A4): before any live frame, a connecting client receives
a display backlog (``MESSAGES_SNAPSHOT``) then the full status read-model
(``STATE_SNAPSHOT``); deltas follow. This pins the emit order and that the client
replays the backlog frames + seeds its status view from the snapshot.

Real instances only — the real emitter, codec, AgUiTransport; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
    STATE_SNAPSHOT,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_connect_emits_messages_then_state_snapshot_then_frames() -> None:
    """Tier 2: the first two SSE events are MESSAGES_SNAPSHOT then STATE_SNAPSHOT,
    the backlog is replayed to the client, and the status view is seeded."""
    backlog = [
        DisplayFrame(OutboxMessage(kind="agent", text="earlier reply")),
    ]

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="agent", text="live reply"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(
        frames(),
        lambda: {"attached_name": "a", "cost_agent": 0.5, "ctx_window": 200},
        backlog=backlog,
    )
    sse = "".join([chunk async for chunk in emitter.stream()])

    # Emit order: MESSAGES_SNAPSHOT then STATE_SNAPSHOT come first (A4).
    events = parse_sse_blocks(sse.split("\n"))
    assert events[0].type == MESSAGES_SNAPSHOT
    assert events[1].type == STATE_SNAPSHOT

    # Client replays the backlog (as a real frame) then the live frame, and
    # seeds its status view from the snapshot.
    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    texts = [
        f.message.text
        async for f in transport.frames()
        if isinstance(f, DisplayFrame) and f.message.kind == "agent"
    ]
    assert texts == ["earlier reply", "live reply"]
    assert transport.status.get("attached_name") == "a"
    assert transport.status.get("ctx_window") == 200
