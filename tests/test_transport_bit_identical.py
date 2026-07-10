"""Tier 2: InProcessTransport is a behavior-preserving refactor (ADR-0039 P1).

The P1 unification moves the inline CUI's two direct render paths (the display
outbox and the renderer's chat-event subscription) behind ONE transport frame
stream — routing only, delivery unchanged. This pins that byte-for-byte:

- the DISPLAY sub-stream rendered THROUGH the transport (repl_outbox → pump →
  frames → run_output_loop → renderer.message) is identical to the direct
  ``renderer.message`` baseline; and
- the EVENT sub-stream (chat_events → filtered subscription → frames →
  renderer.on_chat_event) reproduces the identical WaitingOn / working-indicator
  transition sequence as the direct ``renderer.on_chat_event`` baseline.

The renderer is unchanged; only the source routing moved. Real instances only —
a real ``InlineChatRenderer``, a real ``EventLog`` for chat_events, a real
``repl_outbox`` queue behind a small registry double (no mocks).
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
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage

# A fixed monotonic clock makes the working-indicator fragments a deterministic
# function of the WaitingOn STATE (elapsed = now - since = 0), so a frag-sequence
# comparison IS a state-transition comparison — via the PUBLIC working_frags.
_NOW = 10_000.0

# Display frames spanning every renderer kind branch.
_DISPLAY = [
    OutboxMessage(kind="agent", text="hello [world] <ok>"),
    OutboxMessage(kind="status", text="thinking about it"),
    OutboxMessage(kind="error", text="something failed"),
    OutboxMessage(kind="intervention", text="approve this? [y/N]"),
    OutboxMessage(kind="trace", text="· ran a tool"),
]

# The event sub-stream: the full renderer vocabulary (turn lifecycle + the
# tool-axis WaitingOn table + intervention-answer).
_EVENTS = [
    ("turn_started", {}),
    ("tool_called", {"tool": "grep_files"}),
    ("tool_returned", {}),
    ("tool_failed", {}),
    ("user_answered_intervention", {}),
    ("turn_settled", {}),
]


class _RecordingInlineRenderer(InlineChatRenderer):
    """A real InlineChatRenderer that records the working-indicator state after
    each chat-event (public working_frags, fixed clock → deterministic)."""

    def __init__(self) -> None:
        super().__init__()
        self.working_states: list = []

    def on_chat_event(self, event) -> None:
        super().on_chat_event(event)
        self.working_states.append(self.working_frags(_NOW))


class _FakeRegistry:
    """Registry double: a real EventLog for chat_events + a real repl_outbox
    queue + the focus-listener binding the transport composes. No mocks."""

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
    """Direct-path baseline: renderer.on_chat_event + renderer.message directly."""
    r = _RecordingInlineRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    for etype, data in _EVENTS:
        r.on_chat_event(Event(type=etype, data=data))
    for msg in _DISPLAY:
        r.message(msg)
    return buf.getvalue(), r.working_states


async def _via_transport(monkeypatch) -> tuple[str, list]:
    """The same script routed THROUGH InProcessTransport + run_output_loop."""
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


@pytest.mark.asyncio
async def test_transport_render_and_status_bar_match_direct_path(monkeypatch) -> None:
    """Tier 2: the display bytes AND the WaitingOn transition sequence produced
    through the transport equal the direct-path baseline (behavior-preserving)."""
    monkeypatch.setattr(time, "monotonic", lambda: _NOW)

    base_stdout, base_states = _baseline(monkeypatch)
    tx_stdout, tx_states = await _via_transport(monkeypatch)

    # Display path: identical rendered bytes (renderer unchanged, frames verbatim).
    assert tx_stdout == base_stdout
    assert base_stdout  # sanity: the script actually rendered something

    # Event path: identical WaitingOn / working-indicator transition sequence.
    assert tx_states == base_states
    # sanity: the sequence is non-trivial (Thinking → Running grep_files → …).
    # working_line splits the label into per-character frags, so reconstruct it.
    assert len(base_states) == len(_EVENTS)

    def _label(frags) -> str:
        return "".join(txt for _style, txt in frags)

    running = [frags for frags in base_states if "grep_files" in _label(frags)]
    assert running, "expected a 'Running grep_files' WaitingOn state from tool_called"
