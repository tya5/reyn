"""Tier 2: #5126 (lead-coder catch on #5107, issuecomment-5380819283) — a
genuine exception raised while ``AgUiTransport._pump_sse`` reads the SSE
source (a connection dying mid-stream, e.g. ``httpx.ReadError``) used to be
indistinguishable from a clean end-of-stream: ``_pump_sse``'s ``finally``
enqueued the SAME ``_SSE_DONE`` sentinel on every exit, then let the raised
exception propagate out of the untracked background task, where nothing
ever awaited it (an asyncio "exception was never retrieved" orphan).
``frames()``, seeing only ``_SSE_DONE``, returned exactly as if the stream
had ended normally — a real connection failure vanished silently, mid-turn,
with the caller (a TUI, or a test) none the wiser.

Real ``AgUiTransport`` + a REAL ``AgUiEmitter``-encoded SSE stream throughout
(the same production encoder that writes the wire format, not hand-rolled
SSE text) — the failure is injected by raising out of the hand-fed async
generator AFTER it has yielded genuine, correctly-encoded SSE lines, the
same pattern every other ``AgUiTransport`` test in this suite uses for its
SSE source (e.g. ``test_3300_p2a_queue_state_publish.py``'s ``_sse_lines``).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _InjectedFailure(RuntimeError):
    """Stands in for a real transport-level failure (httpx.ReadError etc.)
    — what actually raises is irrelevant to the property under test (the
    pump must not swallow ANY exception), so a plain exception type avoids
    coupling this test to httpx's own class hierarchy."""


async def _one_display_frame():
    yield DisplayFrame(OutboxMessage(kind="agent", text="hello"))


def _status_provider():
    return None


async def _sse_lines_then_fail(sse_text: str):
    for line in sse_text.split("\n"):
        yield line
    raise _InjectedFailure("connection dropped mid-stream")


async def _noop_send(_payload: dict) -> "dict | None":
    return None


async def _build_real_sse_text() -> str:
    """A REAL, production-encoded SSE stream (one display frame's worth) —
    the same ``AgUiEmitter.stream()`` the actual server route uses."""
    emitter = AgUiEmitter(_one_display_frame(), _status_provider)
    return "".join([chunk async for chunk in emitter.stream()])


@pytest.mark.asyncio
async def test_a_pump_exception_reaches_the_frames_caller_not_swallowed():
    """Tier 2: #5126's own witness — ``frames()`` must RAISE the real
    exception, not return as if the stream ended cleanly.

    Strip-falsifier: reverting ``_pump_sse``'s ``except Exception`` block
    (and ``frames()``'s ``_SSEPumpError`` check) turns this red — the
    ``async for`` below completes with NO exception raised (only the
    STATE_SNAPSHOT/MESSAGES_SNAPSHOT reconnect frames, if any, then a
    silent return), instead of raising ``_InjectedFailure``. Verified
    locally."""
    sse_text = await _build_real_sse_text()
    transport = AgUiTransport(_sse_lines_then_fail(sse_text), _noop_send)

    received = []
    with pytest.raises(_InjectedFailure, match="connection dropped mid-stream"):
        async for frame in transport.frames():
            received.append(frame)

    assert any(
        isinstance(f, DisplayFrame) and f.message.text == "hello" for f in received
    ), (
        "sanity: the frame before the failure must still have been "
        "delivered — this witness is about the FAILURE not being silently "
        "dropped, not about losing frames that arrived before it"
    )


@pytest.mark.asyncio
async def test_a_clean_end_of_stream_still_returns_normally():
    """Tier 2: regression guard — the fix must not turn an ORDINARY
    end-of-stream (the source running dry, no exception) into a raise. Only
    a genuine exception should surface; a clean end is still a clean
    return."""
    sse_text = await _build_real_sse_text()

    async def _sse_lines_clean():
        for line in sse_text.split("\n"):
            yield line

    transport = AgUiTransport(_sse_lines_clean(), _noop_send)
    received = [frame async for frame in transport.frames()]
    assert any(
        isinstance(f, DisplayFrame) and f.message.text == "hello" for f in received
    )
