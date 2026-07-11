"""Tier 2: the reyn CUI is byte-identical across transports (ADR-0039 P2).

P1 proved InProcessTransport == direct baseline. P2 extends the bit-identical
invariant to the WIRE: the same session frame script routed session → server
AG-UI SSE emit → client SSE decode → Frame → renderer produces the IDENTICAL
display bytes AND WaitingOn / working-indicator transition sequence as the
InProcess transport and the direct baseline. If any renderer-relevant field did
not survive the wire round-trip, the bytes or the state sequence would diverge.

Real instances only — a real InlineChatRenderer, a real AgUiEmitter + AgUiTransport
over real SSE text, the real codec; no mocks. Fixed monotonic clock so the
working-indicator fragments are a deterministic function of the WaitingOn state.
"""
from __future__ import annotations

import asyncio
import sys
import time
from io import StringIO

import pytest

from reyn.core.events.events import Event, EventLog
from reyn.interfaces.repl.renderer import InlineChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage

_NOW = 10_000.0

_DISPLAY = [
    OutboxMessage(kind="agent", text="hello [world] <ok>"),
    OutboxMessage(kind="status", text="thinking about it"),
    OutboxMessage(kind="error", text="something failed"),
    OutboxMessage(kind="intervention", text="approve this? [y/N]"),
    OutboxMessage(kind="trace", text="· ran a tool"),
]

_EVENTS = [
    ("turn_started", {}),
    ("tool_called", {"tool": "grep_files"}),
    ("tool_returned", {}),
    ("tool_failed", {}),
    ("user_answered_intervention", {}),
    ("turn_settled", {}),
]

# The interleaved script (events then display then __end__) as Frames — the
# common source both transports consume.
def _script_frames() -> list:
    frames: list = [EventFrame(Event(type=t, data=d)) for t, d in _EVENTS]
    frames += [DisplayFrame(m) for m in _DISPLAY]
    frames.append(DisplayFrame(OutboxMessage(kind="__end__", text="")))
    return frames


class _RecordingInlineRenderer(InlineChatRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.working_states: list = []

    def on_chat_event(self, event) -> None:
        super().on_chat_event(event)
        self.working_states.append(self.working_frags(_NOW))


class _FakeRegistry:
    def __init__(self) -> None:
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        self.chat_events = EventLog()
        self._cb = None

    def bind_focus_listeners(self, *, on_chat_event=None, intervention_channel=None) -> None:
        self._cb = on_chat_event
        if on_chat_event is not None:
            self.chat_events.add_subscriber(on_chat_event)

    def unbind_focus_listeners(self) -> None:
        if self._cb is not None:
            self.chat_events.remove_subscriber(self._cb)
            self._cb = None

    def attached_session(self):
        return None


def _baseline(monkeypatch) -> tuple[str, list]:
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    for etype, data in _EVENTS:
        r.on_chat_event(Event(type=etype, data=data))
    for msg in _DISPLAY:
        r.message(msg)
    return buf.getvalue(), r.working_states


async def _via_in_process(monkeypatch) -> tuple[str, list]:
    fake = _FakeRegistry()
    transport = InProcessTransport(fake, intervention_channel="tui")
    transport.start()
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    try:
        for etype, data in _EVENTS:
            fake.chat_events.emit(etype, **data)
        for msg in _DISPLAY:
            fake.repl_outbox.put_nowait(msg)
        fake.repl_outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        await asyncio.wait_for(run_output_loop(transport, r), timeout=2.0)
    finally:
        transport.close()
    return buf.getvalue(), r.working_states


async def _frame_source(frames):
    for f in frames:
        yield f


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _via_agui(monkeypatch) -> tuple[str, list]:
    # Server side: emit the frame script to AG-UI SSE text.
    emitter = AgUiEmitter(_frame_source(_script_frames()), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    # Client side: decode the SSE back into frames and drive the renderer.

    async def _noop_send(_payload):  # no client→server traffic in this test
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    await asyncio.wait_for(run_output_loop(transport, r), timeout=2.0)
    return buf.getvalue(), r.working_states


@pytest.mark.asyncio
async def test_agui_bytes_and_status_match_in_process_and_baseline(monkeypatch) -> None:
    """Tier 2: display bytes AND WaitingOn sequence are byte-equal across
    AgUi == InProcess == direct baseline (bit-identity extended to the wire)."""
    monkeypatch.setattr(time, "monotonic", lambda: _NOW)

    base_stdout, base_states = _baseline(monkeypatch)
    ip_stdout, ip_states = await _via_in_process(monkeypatch)
    ag_stdout, ag_states = await _via_agui(monkeypatch)

    # sanity: the script actually rendered something non-trivial.
    assert base_stdout
    assert len(base_states) == len(_EVENTS)

    # Display bytes: wire round-trip == in-process == baseline.
    assert ip_stdout == base_stdout
    assert ag_stdout == base_stdout

    # WaitingOn / working-indicator transition sequence: identical across all.
    assert ip_states == base_states
    assert ag_states == base_states
