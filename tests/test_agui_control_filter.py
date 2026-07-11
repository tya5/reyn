"""Tier 2: local-control sentinels are not forwarded on the AG-UI wire (ADR-0039).

reyn's producer outbox carries a few ``__...__`` display sentinels that drive a
LOCAL UI action and have no remote-UI semantics. The AG-UI emitter must NOT put
these on the wire — otherwise a remote client receives an unprofiled ``CUSTOM``
event with nothing to render. This is an EXPLICIT per-entry allowlist
(:data:`~reyn.interfaces.transport.agui.protocol.CONTROL_FILTER_KINDS`), never the
negation of a forward-set (which would wrongly drop renderable display kinds).

Two halves pinned here:

- **Filtered**: ``__copy_last_reply__`` / ``__rewind_list__`` /
  ``__session_switch_request__`` / ``__end__`` produce ZERO wire events; ``__end__``
  additionally terminates the stream.
- **Not filtered**: ``__attach_request__`` IS forwarded (a profiled ``CUSTOM``
  display event) — the TUI ``--connect`` attach-label sync (F13 #303) needs it
  delivered remotely. Ordinary display frames (``agent``) are unaffected.

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
async def test_local_control_sentinels_are_not_on_the_wire() -> None:
    """Tier 2: the three purely-local sentinels produce ZERO wire events; the
    ``agent`` frame around them is forwarded normally (the filter is per-kind, not
    a stream-wide drop)."""
    frames = [
        DisplayFrame(OutboxMessage(kind="__copy_last_reply__", text="c")),
        DisplayFrame(OutboxMessage(kind="agent", text="hello")),
        DisplayFrame(OutboxMessage(kind="__rewind_list__", text="r")),
        DisplayFrame(OutboxMessage(kind="__session_switch_request__", text="s")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    names = _reyn_display_names(events)

    for sentinel in ("__copy_last_reply__", "__rewind_list__", "__session_switch_request__"):
        assert f"reyn.display.{sentinel}" not in names, sentinel
    assert "reyn.display.__end__" not in names
    # The ordinary agent frame between them IS forwarded (its text triplet).
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


@pytest.mark.asyncio
async def test_attach_request_is_forwarded_as_profiled_custom() -> None:
    """Tier 2: ``__attach_request__`` is the exception — it IS forwarded (a
    ``CUSTOM`` display event), NOT filtered, per the F13 #303 remote need."""
    assert "__attach_request__" not in CONTROL_FILTER_KINDS
    frames = [
        DisplayFrame(OutboxMessage(kind="__attach_request__", text="agent-b")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    assert "reyn.display.__attach_request__" in _reyn_display_names(events)
