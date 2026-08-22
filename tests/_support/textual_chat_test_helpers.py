"""Shared TextualChatApp test helpers: a queue-backed transport double and a
presenter that records the widths it was asked to render at.

``started`` builds a ``tool_call_started`` outbox message. ``QueueTransport``
is a ``ClientTransport`` double that hands scripted display frames off an
``asyncio.Queue``. ``WidthRecordingPresenter`` wraps ``ReynPresenter`` and
records each width it presents at, for tests asserting on layout reflow.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from textual_flowview import Entry

from reyn.interfaces.inline.textual_chat import ReynPresenter
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


def started(op_id: str, tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started", text=tool, meta={"tool": tool, "op_id": op_id, "args": {}}
    )


class QueueTransport(ClientTransportStub):
    """A real :class:`ClientTransport` fed one frame at a time from a queue, so a
    test can push a ``started`` frame, inspect the RUNNING row, THEN push the
    completion and inspect the settle — with the stream staying open in between
    (mirrors ``test_textual_chat_phase2b_live_tool_3283.py``'s helper)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[OutboxMessage]" = asyncio.Queue()
        self.submitted: list[str] = []

    async def push(self, msg: OutboxMessage) -> None:
        await self._queue.put(msg)

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            msg = await self._queue.get()
            yield DisplayFrame(msg)

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class WidthRecordingPresenter(ReynPresenter):
    """A real :class:`ReynPresenter` SUBCLASS (not a mock) that records the
    ``width`` FlowView hands ``present()`` for each entry — this is the BODY
    width (content width minus BOTH gutters), the one surface that actually
    exposes what the right gutter's column cost the conversation content.
    ``FlowView.region.width`` stays the FULL terminal width regardless of
    gutter configuration (gutter consumption is internal to flowview and not
    otherwise observable from outside it) — a co-vet finding on #3337, this
    class is the fix: it reads the real value off the real collaboration
    seam, not a private flowview attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.widths: "list[int]" = []

    async def present(self, entry: "Entry[OutboxMessage]", width: int):
        self.widths.append(width)
        return await super().present(entry, width)
