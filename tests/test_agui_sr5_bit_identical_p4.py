"""Tier 2: SR5 — the reyn client render is byte-identical across the P4 change.

P4 widens the STANDARD surface (the text triplet, the standard messages array,
the tool status field) so a generic client renders a functional chat. SR5 is the
standing gate: every P4 change is ADDITIVE to the standard surface, ``_reyn``
stays byte-unchanged, and the reyn client's render is unaffected.

This pins both sides of that gate in one run:

- the P4 wire genuinely carries the new generic scaffold — the
  ``TEXT_MESSAGE_START`` / ``TEXT_MESSAGE_END`` events are present; yet
- the reyn client, decoding the SAME wire, renders **byte-identical** display
  output and an identical WaitingOn / working-indicator sequence to the direct
  baseline (the renderer driven with no transport at all).

If ``_reyn`` byte-equality broke, the decoded frames would differ and these
byte-equalities would go RED. (The broader P2/P3 bit-identical suite stays green
alongside — it is the InProcess == AgUi == baseline invariant.)

Real instances only — a real InlineChatRenderer, a real AgUiEmitter + AgUiTransport
over real SSE text; no mocks. Fixed monotonic clock so the working-indicator
fragments are a deterministic function of the WaitingOn state.
"""
from __future__ import annotations

import asyncio
import sys
import time
from io import StringIO

import pytest

from reyn.core.events.events import Event
from reyn.interfaces.repl.renderer import InlineChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.protocol import (
    TEXT_MESSAGE_END,
    TEXT_MESSAGE_START,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage

_NOW = 10_000.0

_DISPLAY = [
    OutboxMessage(kind="agent", text="hello [world] <ok>"),
    OutboxMessage(kind="status", text="thinking about it"),
    OutboxMessage(kind="error", text="something failed"),
]

_EVENTS = [
    ("turn_started", {}),
    ("tool_called", {"tool": "grep_files"}),
    ("tool_failed", {}),
    ("turn_settled", {}),
]


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


def _baseline(monkeypatch) -> tuple[str, list]:
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    for etype, data in _EVENTS:
        r.on_chat_event(Event(type=etype, data=data))
    for msg in _DISPLAY:
        r.message(msg)
    return buf.getvalue(), r.working_states


async def _frame_source(frames):
    for f in frames:
        yield f


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


@pytest.mark.asyncio
async def test_sr5_wire_has_triplet_yet_reyn_render_matches_baseline(monkeypatch) -> None:
    """Tier 2: SR5 — the P4 wire carries the text triplet scaffold, yet the reyn
    client's render (display bytes + WaitingOn sequence) is byte-identical to the
    direct baseline — the standard-surface widening did not perturb reyn."""
    monkeypatch.setattr(time, "monotonic", lambda: _NOW)

    base_stdout, base_states = _baseline(monkeypatch)

    # Server side: emit the frame script to AG-UI SSE text (P4 emitter).
    emitter = AgUiEmitter(_frame_source(_script_frames()), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    # The generic scaffold is genuinely present on the wire (else the SR5
    # byte-equality below would be vacuous — the triplet must exist).
    wire_types = {ev.type for ev in parse_sse_blocks(sse.split("\n"))}
    assert TEXT_MESSAGE_START in wire_types
    assert TEXT_MESSAGE_END in wire_types

    # Client side: decode the SAME wire and drive the reyn renderer.
    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    await asyncio.wait_for(run_output_loop(transport, r), timeout=2.0)
    ag_stdout, ag_states = buf.getvalue(), r.working_states

    # Sanity: the script rendered something non-trivial.
    assert base_stdout

    # SR5: byte-identical display + identical WaitingOn sequence, triplet notwithstanding.
    assert ag_stdout == base_stdout
    assert ag_states == base_states
