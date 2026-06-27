"""Tier 2: _output_loop contains a single message's render failure.

The output loop is the sole consumer of the registry's repl_outbox, so an
uncaught exception while rendering one message would end the loop and tear the
whole REPL down. This drives the loop with a real queue and a recorder renderer
that raises on one poison message, and asserts later messages still render (the
loop survived) — before the fix the exception propagated out of _output_loop.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from reyn.interfaces.repl.repl import _output_loop
from reyn.runtime.outbox import OutboxMessage


class _Recorder:
    """A real renderer double: records rendered text, raises on a poison msg."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def message(self, msg: OutboxMessage) -> None:
        if msg.text == "BOOM":
            raise RuntimeError("simulated render failure")
        self.seen.append(msg.text)


@pytest.mark.asyncio
async def test_output_loop_survives_a_render_exception() -> None:
    """Tier 2: a message whose render raises is skipped; the loop keeps
    draining and renders the messages after it."""
    queue: asyncio.Queue = asyncio.Queue()
    registry = SimpleNamespace(repl_outbox=queue)
    renderer = _Recorder()
    for text, kind in [
        ("before", "agent"),
        ("BOOM", "agent"),       # render raises here
        ("after", "agent"),
        ("", "__end__"),
    ]:
        await queue.put(OutboxMessage(kind=kind, text=text))

    # Before the fix this raises RuntimeError out of the loop; after, it returns.
    await asyncio.wait_for(_output_loop(registry, renderer), timeout=2.0)

    assert "before" in renderer.seen
    assert "after" in renderer.seen      # the loop continued past the failure
    assert "BOOM" not in renderer.seen   # the failing render recorded nothing
