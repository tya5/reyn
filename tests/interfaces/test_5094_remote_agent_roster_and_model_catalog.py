"""Tier 2: #5094 — a remote client's agent tab and model-class picker
reflect the SERVER's real roster, not an unconditional empty literal.

Owner live-blocked on this (relayed via architect/lead-coder): connecting 2
agents (`--connect x2`, both `default`) showed nothing in the TUI's agent
tab despite the workspace genuinely having 4 agents. Root cause,
measured: `agui/state.py`'s own ``_WIRE_KEYS`` filter never forwarded
`agent_names`/`session_tree`/`model_active_class`/`model_classes` past the
server's projection, even though `status._snapshot`'s own
``registry.loaded_names()``/``registry.session_tree()``/``Session.
active_model_class()``/``known_model_classes()`` calls already compute
real values server-side — so ``project_remote_snapshot`` (client-side) had
nothing to read and fell back to a hand-typed `[]`/`None` literal
regardless of how many agents/model classes the server actually had.

Same real, end-to-end pattern
`test_agui_state_read_model.py::test_remote_status_view_reflects_snapshot_
then_delta` already establishes for this wire (real ``AgUiEmitter`` → real
SSE text → real ``AgUiTransport`` parsing it back) — extended through
`project_remote_snapshot` (the read-model projection the TUI's own chips
actually read), not stopping at the raw wire dict.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.repl.read_model import project_remote_snapshot
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_agent_roster_and_model_catalog_ride_the_wire_end_to_end():
    """Tier 2: the acceptance witness architect specified — a remote
    client's agent tab must show the ACTUAL count and names the server
    has, not merely "non-empty". Drives the real emitter → real SSE →
    real transport → the real `project_remote_snapshot` projection the
    TUI's own chrome reads, exactly the path a live `--connect` uses."""
    state = {
        "cost_agent": 1.0, "cost_total": 1.0, "ctx_used": 10, "ctx_window": 100,
        "agent_tokens": 5, "attached_name": "default", "model": "m",
        "agent_names": ["default", "neo", "coder-smith", "coder-brown"],
        "session_tree": [
            {"agent": "default"}, {"agent": "neo"},
            {"agent": "coder-smith"}, {"agent": "coder-brown"},
        ],
        "model_active_class": "standard",
        "model_classes": ["light", "standard", "strong"],
    }

    def status_provider():
        return dict(state)

    async def frames():
        yield OutboxMessage(kind="agent", text="done")
        yield OutboxMessage(kind="__end__", text="")

    async def _display_frames():
        async for msg in frames():
            yield DisplayFrame(msg)

    emitter = AgUiEmitter(_display_frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass  # draining applies STATE_SNAPSHOT to transport.status

    wire_values = transport.status.values
    projected = project_remote_snapshot(wire_values)

    assert projected["agent_names"] == ["default", "neo", "coder-smith", "coder-brown"], (
        f"expected all 4 real agent names, got {projected['agent_names']!r}"
    )
    assert projected["session_tree"] == [
        {"agent": "default"}, {"agent": "neo"},
        {"agent": "coder-smith"}, {"agent": "coder-brown"},
    ]
    assert projected["model_active_class"] == "standard"
    assert projected["model_classes"] == ["light", "standard", "strong"]
    # The 3 new capability flags must say "reported", not silently omit —
    # a consumer distinguishing "genuinely empty" from "unsupported" reads
    # these, not the bare data keys.
    assert projected["agent_roster_reported"] is True
    assert projected["model_catalog_reported"] is True
    assert projected["attached_name_reported"] is True


@pytest.mark.asyncio
async def test_strip_falsifier_removing_the_wire_keys_reverts_to_the_old_empty_bug():
    """Tier 2: strip-falsifier — with the 4 keys absent from the server's
    OWN status dict (simulating `_WIRE_KEYS` reverted to not carry them,
    the exact pre-#5094 shape), the client falls back to the SAME
    graceful-empty defaults the bug reproduced — confirming the positive
    witness above genuinely depends on the wire carrying real data, not on
    `project_remote_snapshot`'s own defaults happening to look right."""
    state = {
        "cost_agent": 1.0, "cost_total": 1.0, "ctx_used": 10, "ctx_window": 100,
        "agent_tokens": 5, "attached_name": "default", "model": "m",
        # agent_names/session_tree/model_active_class/model_classes
        # deliberately absent — the pre-#5094 server-side shape.
    }

    def status_provider():
        return dict(state)

    async def _display_frames():
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(_display_frames(), status_provider)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass

    projected = project_remote_snapshot(transport.status.values)
    assert projected["agent_names"] == []
    assert projected["session_tree"] == []
    assert projected["model_active_class"] is None
    assert projected["model_classes"] == []
