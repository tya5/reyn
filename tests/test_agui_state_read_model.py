"""Tier 2: the remote status panel reflects server STATE_* values (P2 hard gate).

P1 left the status bar read registry-duck-typed — fine in-process, broken over
the wire. P2 streams the session status view (ctx / cost / token / WaitingOn) as
``STATE_SNAPSHOT`` on connect + ``STATE_DELTA`` on change. This pins the hard
gate: after the server emits a snapshot and at least one delta, the CLIENT's
remote status view reflects the SERVER's values — both the snapshot baseline and
the delta-updated value.

The status source is a read-model (the projected snapshot subset), NOT a file
mirror. Real instances only — the real emitter, codec, AgUiTransport, and the
RemoteStatusView; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_remote_status_view_reflects_snapshot_then_delta() -> None:
    """Tier 2: the client status view carries the server's snapshot values, then
    a delta updates them (WaitingOn from the chat-event stream + a cost change)."""
    # A mutable server-side status source: cost rises between frames, so the
    # emitter produces a STATE_DELTA. WaitingOn is derived from the event stream.
    state = {"cost_agent": 1.0, "cost_total": 1.0, "ctx_used": 10, "ctx_window": 100,
             "agent_tokens": 5, "attached_name": "a", "model": "m"}

    def status_provider():
        return dict(state)

    async def frames():
        # A tool_called event advances WaitingOn to "Running grep_files"; then a
        # cost change drives a STATE_DELTA on the next frame.
        yield EventFrame(Event(type="turn_started", data={}))
        yield EventFrame(Event(type="tool_called", data={"tool": "grep_files"}))
        state["cost_agent"] = 2.5  # server-side change → delta
        yield DisplayFrame(OutboxMessage(kind="agent", text="done"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])

    # Sanity: the wire actually carried a snapshot AND a delta.
    assert "STATE_SNAPSHOT" in sse
    assert "STATE_DELTA" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass  # draining also applies STATE_* to transport.status

    view = transport.status
    # Snapshot baseline reached the client...
    assert view.get("attached_name") == "a"
    assert view.get("ctx_window") == 100
    # ...and the delta updated the changed keys (cost + WaitingOn).
    assert view.get("cost_agent") == 2.5
    assert view.get("waiting_on") == "Running grep_files"
