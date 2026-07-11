"""Tier 2: remote /rewind degrades to a text list and does NOT hang (ADR-0039 P6b).

The interactive ↑↓ rewind picker is driven by ``session.pending_command_ui`` —
inline-app-local state that is NOT carried on the AG-UI ``STATE_*`` channel. So a
remote thin client (``reyn chat --connect``, which renders with a
``ConsoleChatRenderer`` whose ``uses_app_input()`` is False) must take the
text-list fallback in ``run_output_loop``: the ``__rewind_list__`` frame renders
as a plain intervention list and the loop still terminates on ``__end__`` — it
does not block waiting for a picker frame that never arrives.

Real AgUiEmitter → real SSE text → real AgUiTransport decode → real
ConsoleChatRenderer, no mocks (the same wire the ``--connect`` client runs).
"""
from __future__ import annotations

import asyncio
import sys
from io import StringIO

import pytest

from reyn.interfaces.repl.renderer import ConsoleChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame, Frame
from reyn.runtime.outbox import OutboxMessage

_REWIND_TEXT = "rewind points:\n  1) turn-3 abc\n  2) turn-2 def\n  3) turn-1 ghi"


def _script() -> list[Frame]:
    return [
        DisplayFrame(OutboxMessage(kind="__rewind_list__", text=_REWIND_TEXT)),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]


async def _frame_source(frames):
    for f in frames:
        yield f


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


async def _run_remote(renderer) -> str:
    # Server: encode the frame script to AG-UI SSE. Client: decode + drive the
    # renderer exactly as ``reyn chat --connect`` does.
    emitter = AgUiEmitter(_frame_source(_script()), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    buf = StringIO()
    old = sys.__stdout__
    sys.__stdout__ = buf  # ConsoleChatRenderer writes here
    try:
        # A finite timeout is the no-hang assertion: if the fallback branch were
        # skipped AND the loop blocked on a never-arriving picker, this raises.
        await asyncio.wait_for(run_output_loop(transport, renderer), timeout=2.0)
    finally:
        sys.__stdout__ = old
    return buf.getvalue()


@pytest.mark.asyncio
async def test_remote_rewind_renders_text_list_without_hanging() -> None:
    """Tier 2: a ConsoleChatRenderer (the --connect renderer, uses_app_input
    False) renders the rewind list text over the wire and the loop terminates."""
    out = await _run_remote(ConsoleChatRenderer())
    # The list content survived the wire and rendered (text fallback, not skipped).
    assert "turn-3 abc" in out
    assert "turn-1 ghi" in out


class _AppInputRenderer(ConsoleChatRenderer):
    """A renderer that claims its own input Application (the inline TTY case)."""

    def uses_app_input(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_app_input_renderer_with_local_region_skips_the_text_list() -> None:
    """Tier 2: the fallback branch is load-bearing — a uses_app_input renderer
    WITH a local command-UI region (command_ui_region=True, the LOCAL inline case)
    SKIPS the text list because its ``pending_command_ui`` drives the region
    selector instead."""
    # command_ui_region defaults to True → the local inline case.
    out = await _run_remote(_AppInputRenderer())
    assert "turn-3 abc" not in out


@pytest.mark.asyncio
async def test_app_input_renderer_without_region_takes_text_fallback() -> None:
    """Tier 2: (ADR-0039 P3) a uses_app_input renderer WITHOUT a local command-UI
    region (command_ui_region=False, the REMOTE inline client) must STILL render
    the rewind list as text — command-UI is not on the AG-UI wire, so a remote
    inline client has no picker to defer to and would otherwise swallow /rewind."""
    emitter = AgUiEmitter(_frame_source(_script()), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    buf = StringIO()
    old = sys.__stdout__
    sys.__stdout__ = buf
    try:
        await asyncio.wait_for(
            run_output_loop(transport, _AppInputRenderer(), command_ui_region=False),
            timeout=2.0,
        )
    finally:
        sys.__stdout__ = old
    out = buf.getvalue()
    assert "turn-3 abc" in out
    assert "turn-1 ghi" in out
