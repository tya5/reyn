"""Tier 2: the AG-UI emitter's control-sentinel disposition (ADR-0039 P6a).

A few ``__…__`` display sentinels get per-entry dispositions on the AG-UI wire:

- **Client-consumed → FORWARDED** (profiled ``CUSTOM``): ``__copy_last_reply__`` /
  ``__rewind_list__`` are consumed by the CLIENT over the transport stream (a real
  client-side clipboard copy / rewind picker). In the thin-client model transport
  IS the AG-UI wire, so they MUST reach it — filtering them would make remote
  ``/copy`` / ``/rewind`` silent no-ops.
- **Filtered** (``CONTROL_FILTER_KINDS``, an explicit per-entry allowlist — never
  the negation of a forward-set): ``__end__`` (the stream terminator).

``__attach_request__`` / ``__session_switch_request__`` both retired (#4534
PR-2 / PR-2b): ``/attach`` and ``/session switch`` now go through
``ClientTransport.request_attach`` / ``request_session_switch``, typed
operations with no display-channel sentinel — neither kind is constructed
anywhere anymore, so this file's former reachability tests for them
(measuring "forwarder swallows it for ``repl_outbox`` yet it still reaches
the wire" and "the tap, not the filter, keeps the switch sentinel off the
wire") no longer have a subject and are deleted. Session-switch follow's own
mechanism (``registry.add_attach_listener`` → ``_SessionFrameSource``'s
dual-wait) has its own coverage in ``test_3310_n3_remote_switch_parity.py``.

Real instances only — a real ``AgUiEmitter`` over real SSE text; no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    CONTROL_FILTER_KINDS,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _frame_source(frames):
    for f in frames:
        yield f


async def _wire_events(frames):
    emitter = AgUiEmitter(_frame_source(frames), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    return parse_sse_blocks(sse.split("\n"))


def _reyn_display_names(events) -> set[str]:
    names: set[str] = set()
    for ev in events:
        data = ev.data or {}
        reyn = data.get("_reyn") if isinstance(data, dict) else None
        if isinstance(reyn, dict) and reyn.get("frame") == "display":
            names.add(f"reyn.display.{reyn.get('kind')}")
    return names


@pytest.mark.asyncio
async def test_client_consumed_sentinels_are_forwarded_on_the_wire() -> None:
    """Tier 2: wire-existence probe — ``__copy_last_reply__`` / ``__rewind_list__``
    ARE forwarded (a NON-zero AG-UI event each), because the client consumes them
    over the transport stream. Filtering them would break remote /copy and /rewind."""
    frames = [
        DisplayFrame(OutboxMessage(kind="__copy_last_reply__", text="c")),
        DisplayFrame(OutboxMessage(kind="__rewind_list__", text="r")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    names = _reyn_display_names(events)

    assert "reyn.display.__copy_last_reply__" in names
    assert "reyn.display.__rewind_list__" in names
    # Neither is in the filter set (the disposition backing the forward).
    assert "__copy_last_reply__" not in CONTROL_FILTER_KINDS
    assert "__rewind_list__" not in CONTROL_FILTER_KINDS


@pytest.mark.asyncio
async def test_end_sentinel_terminates_the_stream() -> None:
    """Tier 2: ``__end__`` terminates the stream — frames after it are never
    emitted (the emitter returns on the sentinel)."""
    frames = [
        DisplayFrame(OutboxMessage(kind="agent", text="before end")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
        DisplayFrame(OutboxMessage(kind="agent", text="AFTER end — must not appear")),
    ]
    events = await _wire_events(frames)
    blob = "".join(str(ev.data) for ev in events)
    assert "before end" in blob
    assert "AFTER end" not in blob
