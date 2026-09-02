"""Tier 2: #5694 — a genuine ``AgUiTransport`` pump failure (its SSE read
loop dying mid-session, e.g. ``httpx.ReadError``) must flip the SAME
``has_session()``/``attach_failed()`` pair the header and the composer
submit-gate already share (#3671 P3), so a caller that only catches the
exception ``_pump_frames`` raises (#5329: log it, keep the app open, do not
exit) still learns the connection died — instead of the transport looking
"still attached" while the SERVER's own per-connection binding
(``_CONNECTION_RETARGET_HUB``, #5116/#5129) has already forgotten this
connection and a later submit silently falls back to the connect-time URL
agent (200 OK, wrong destination — the exact symptom #5694 reports, root-
caused against a real production incident's own audit-event log).

Before this PR, ``AgUiTransport.has_session()``/``attach_failed()`` tracked
ONLY the intentional-``close()`` case (``self._connected``) and a hardcoded
``False`` respectively — a pump death after a successful attach was
invisible to both: the display kept drawing the last-known agent name
forever, and the composer kept accepting (and POSTing) new turns through the
now-orphaned ``send`` closure.

This is the acceptance-item-3 witness (issue #5694, "a test that goes red
the moment a second independent source is reintroduced"): ``has_session``/
``attach_failed`` must derive from the ONE fact ``frames()`` already learns
when it re-raises a genuine ``_SSEPumpError`` — not a second, independently
maintained "am I connected" flag that could drift from it. Strip-falsifier:
removing the ``self._connected = False`` / ``self._pump_died = True`` lines
``AgUiTransport.frames()`` sets right before re-raising turns this test red
(``has_session()`` stays True and ``attach_failed()`` stays False forever,
even after the pump has genuinely died) — verified locally.

Real ``AgUiTransport`` + a REAL ``AgUiEmitter``-encoded SSE stream (mirrors
``test_5126_pump_sse_exception_not_swallowed.py``'s own technique, not a
hand-rolled SSE text)."""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _InjectedFailure(RuntimeError):
    """Stands in for a real transport-level failure — what actually raises
    is irrelevant to the property under test, mirrors #5126's own test."""


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
    emitter = AgUiEmitter(_one_display_frame(), _status_provider)
    return "".join([chunk async for chunk in emitter.stream()])


@pytest.mark.asyncio
async def test_pump_death_flips_has_session_and_attach_failed_together():
    """Tier 2: the core witness — before the pump dies, ``has_session()`` is
    True (constructed ``connected=True``, the production default) and
    ``attach_failed()`` is False; after ``frames()`` raises the real
    exception, BOTH flip — from the SAME event, not two separately-driven
    flags."""
    sse_text = await _build_real_sse_text()
    transport = AgUiTransport(_sse_lines_then_fail(sse_text), _noop_send)

    assert transport.has_session() is True
    assert transport.attach_failed() is False

    received = []
    with pytest.raises(_InjectedFailure, match="connection dropped mid-stream"):
        async for frame in transport.frames():
            received.append(frame)

    assert any(
        isinstance(f, DisplayFrame) and f.message.text == "hello" for f in received
    ), "sanity: the frame before the failure must still have been delivered"

    assert transport.has_session() is False, (
        "a caller that (like _pump_frames, #5329) only catches this "
        "exception around its own read loop must still see has_session() "
        "go False — otherwise the header/pane keep drawing the last-known "
        "agent name forever"
    )
    assert transport.attach_failed() is True, (
        "must read as FAILED, not merely 'not yet attached' — this app "
        "never auto-reconnects, so a 'connecting' render here would be "
        "the indefinite-loading paper-over the #3671 P3 owner ruling "
        "forbids"
    )


@pytest.mark.asyncio
async def test_a_clean_end_of_stream_does_not_flip_attach_failed():
    """Tier 2: falsify pair — an ORDINARY end-of-stream (the source running
    dry, no exception — #5126's own regression guard) must NOT be
    mistaken for a pump death. ``attach_failed()`` stays False; only a
    genuine exception sets it."""
    sse_text = await _build_real_sse_text()

    async def _sse_lines_clean():
        for line in sse_text.split("\n"):
            yield line

    transport = AgUiTransport(_sse_lines_clean(), _noop_send)
    received = [frame async for frame in transport.frames()]
    assert any(
        isinstance(f, DisplayFrame) and f.message.text == "hello" for f in received
    )
    assert transport.attach_failed() is False
    assert transport.has_session() is True, (
        "a clean end-of-stream is not a failure — has_session() must not "
        "be conflated with 'the frames() generator has been exhausted'"
    )
