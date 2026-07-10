"""Tier 2: run_output_loop contains a single message's render failure.

The output loop is the sole consumer of the transport's unified frame stream,
so an uncaught exception while rendering one display frame would end the loop
and tear the whole REPL down. This drives the loop with a real frame stream and
a recorder renderer that raises on one poison message, and asserts later
messages still render (the loop survived) — before the fix the exception
propagated out of the loop.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _FrameStreamTransport:
    """A real ClientTransport double: yields a fixed sequence of display frames.

    Only ``frames()`` is exercised by run_output_loop; the send side is unused
    here, so this Fake implements just the consumed surface (no mocks).
    """

    def __init__(self, messages: list[OutboxMessage]) -> None:
        self._messages = messages

    async def frames(self):
        for msg in self._messages:
            yield DisplayFrame(msg)


class _Recorder:
    """A real renderer double: records rendered text, raises on a poison msg."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def message(self, msg: OutboxMessage) -> None:
        if msg.text == "BOOM":
            raise RuntimeError("simulated render failure")
        self.seen.append(msg.text)

    def on_chat_event(self, event) -> None:  # pragma: no cover - unused here
        pass

    def uses_app_input(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_output_loop_survives_a_render_exception() -> None:
    """Tier 2: a message whose render raises is skipped; the loop keeps
    draining and renders the messages after it."""
    messages = [
        OutboxMessage(kind="agent", text="before"),
        OutboxMessage(kind="agent", text="BOOM"),   # render raises here
        OutboxMessage(kind="agent", text="after"),
        OutboxMessage(kind="__end__", text=""),
    ]
    transport = _FrameStreamTransport(messages)
    renderer = _Recorder()

    # Before the fix this raises RuntimeError out of the loop; after, it returns.
    await asyncio.wait_for(run_output_loop(transport, renderer), timeout=2.0)

    assert "before" in renderer.seen
    assert "after" in renderer.seen      # the loop continued past the failure
    assert "BOOM" not in renderer.seen   # the failing render recorded nothing
