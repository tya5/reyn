"""Tier 2: the AG-UI emitter's control-sentinel disposition (ADR-0039 P6a).

A few ``__…__`` display sentinels get per-entry dispositions on the AG-UI wire:

- **Client-consumed → FORWARDED** (profiled ``CUSTOM``): ``__copy_last_reply__`` /
  ``__rewind_list__`` are consumed by the CLIENT over the transport stream (a real
  client-side clipboard copy / rewind picker). In the thin-client model transport
  IS the AG-UI wire, so they MUST reach it — filtering them would make remote
  ``/copy`` / ``/rewind`` silent no-ops.
- **Filtered** (``CONTROL_FILTER_KINDS``, an explicit per-entry allowlist — never
  the negation of a forward-set): ``__end__`` (the stream terminator) and
  ``__session_switch_request__`` (already swallowed upstream at registry.py:3061 —
  a fail-safe) produce ZERO wire events.
- ``__attach_request__`` is upstream-consumed at registry.py:3052; it never
  reaches the tap, so its emitter disposition is moot — the profiled fail-safe
  means it would be forwarded if the tap point ever changed.

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
async def test_filtered_control_sentinels_are_not_on_the_wire() -> None:
    """Tier 2: the filtered sentinels (``__session_switch_request__`` / ``__end__``)
    produce ZERO wire events; a surrounding ``agent`` frame is forwarded normally
    (the filter is per-kind, not a stream-wide drop)."""
    frames = [
        DisplayFrame(OutboxMessage(kind="__session_switch_request__", text="s")),
        DisplayFrame(OutboxMessage(kind="agent", text="hello")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    names = _reyn_display_names(events)

    assert "reyn.display.__session_switch_request__" not in names
    assert "reyn.display.__end__" not in names
    assert "reyn.display.agent" in names


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
