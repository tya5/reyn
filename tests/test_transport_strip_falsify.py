"""Tier 2: the transport's chat-event forward-set is LOAD-BEARING (ADR-0039 P1).

Strip-falsify (the P1 analog of the strip-verification discipline): an
``InProcessTransport`` whose forward-set is emptied delivers ZERO chat-events to
the client, so the WaitingOn / Working / Running transitions vanish — RED. With
the DERIVED forward-set (production default) the same chat-events reach the
renderer. This proves the dual-stream event path is a real seam, not decorative
— the structural counter-evidence for the A2 "outbox-only drops WaitingOn" bug.

Real instances only — a real ``EventLog`` for chat_events + a real repl_outbox
queue behind a small registry double; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.frames import renderer_chat_events
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.outbox import OutboxMessage

_EVENTS = [("turn_started", {}), ("tool_called", {"tool": "grep_files"}), ("turn_settled", {})]


class _EventRecordingRenderer:
    """Records the chat-event types the client actually delivers (real double)."""

    def __init__(self) -> None:
        self.events_seen: list[str] = []

    def on_chat_event(self, event) -> None:
        self.events_seen.append(getattr(event, "type", None))

    def message(self, msg) -> None:  # pragma: no cover - display path unused here
        pass

    def uses_app_input(self) -> bool:
        return False


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


async def _drive(forward_events) -> list[str]:
    fake = _FakeRegistry()
    transport = InProcessTransport(
        fake, intervention_channel="tui", forward_events=forward_events
    )
    transport.start()
    renderer = _EventRecordingRenderer()
    try:
        for etype, data in _EVENTS:
            fake.chat_events.emit(etype, **data)
        fake.repl_outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        await asyncio.wait_for(run_output_loop(transport, renderer), timeout=2.0)
    finally:
        transport.close()
    return renderer.events_seen


@pytest.mark.asyncio
async def test_derived_forward_set_delivers_chat_events() -> None:
    """Tier 2: with the derived forward-set, the client receives the WaitingOn
    chat-events (the event path is wired)."""
    seen = await _drive(renderer_chat_events())
    assert set(seen) == {"turn_started", "tool_called", "turn_settled"}


@pytest.mark.asyncio
async def test_stripped_forward_set_makes_waiting_on_vanish() -> None:
    """Tier 2: strip-falsify — with the forward-set emptied, the client receives
    NO chat-events → WaitingOn transitions vanish → RED for the event path."""
    seen = await _drive(frozenset())
    assert seen == [], (
        "stripping the chat-event forward-set must drop ALL renderer chat-events; "
        f"got {seen!r} — the event path is not actually gated by the forward-set"
    )
