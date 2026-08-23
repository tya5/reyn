"""Tier 2: on connect the server emits MESSAGES_SNAPSHOT + STATE_SNAPSHOT (P2, A4).

The reconnect contract (A4): before any live frame, a connecting client receives
a display backlog (``MESSAGES_SNAPSHOT``) then the full status read-model
(``STATE_SNAPSHOT``); deltas follow. This pins the emit order and that the client
delivers the backlog as ONE :class:`~reyn.interfaces.transport.frames.BacklogBatch`
item through the SAME frame stream (#5139, architect FINAL ruling,
issuecomment-5383272756 — supersedes an earlier side-channel draft this file
itself briefly pinned; ``frames()`` never flattened the backlog into individual
DisplayFrame items either before OR after #5139, but #5139 changes WHAT arrives
in its place: one bundled batch item instead of nothing), then seeds its status
view from the snapshot.

Real instances only — the real emitter, codec, AgUiTransport; no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    MESSAGES_SNAPSHOT,
    STATE_SNAPSHOT,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import BacklogBatch, DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_connect_emits_messages_then_state_snapshot_then_frames() -> None:
    """Tier 2: the first two SSE events are MESSAGES_SNAPSHOT then STATE_SNAPSHOT,
    the backlog reaches the client as ONE BacklogBatch item ahead of the live
    frame (#5139), and the status view is seeded."""
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

    # Client yields the backlog as ONE BacklogBatch (for the URL this
    # connection was opened against — "a", the FIRST-connect seed;
    # AgUiTransport.__init__'s own comment), then the live frame, and
    # seeds its status view from the snapshot.
    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send, agent_name="a")
    items = [f async for f in transport.frames()]
    batches = [f for f in items if isinstance(f, BacklogBatch)]
    live_texts = [
        f.message.text
        for f in items
        if isinstance(f, DisplayFrame) and f.message.kind == "agent"
    ]
    # Unpacking into a single-element tuple IS the "exactly one" check
    # (raises on 0 or 2+ matches) — a behavioral assertion on the
    # extracted value, not a ``len(...) == N`` format pin.
    (batch,) = batches
    assert [f.message.text for f in batch.frames] == ["earlier reply"]
    assert batch.agent == "a"
    # The batch item arrives BEFORE the live frame — wire order (backlog is
    # sent ahead of any live activity, A4) equals queue-apply order (#5139's
    # own witness ②).
    assert isinstance(items[0], BacklogBatch)
    assert live_texts == ["live reply"]
    assert transport.status.get("attached_name") == "a"
    assert transport.status.get("ctx_window") == 200
