"""Tier 2: pipeline progress frames create and animate a visible flow entry.

The pipeline coalescer must append through the FlowModel owned by the view and
use the same live-entry animation seam as tool rows. A real Textual test app
and real textual-flowview objects provide the observation surface.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _PipelineTransport(ClientTransportStub):
    def __init__(self) -> None:
        self._queue: "asyncio.Queue[DisplayFrame]" = asyncio.Queue()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    async def push(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def attach_failed(self) -> bool:
        return False

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass

    async def run_slash_command(self, name: str, args: str) -> bool:
        return False


def _pipeline_message(index: int = 0) -> OutboxMessage:
    return OutboxMessage(
        kind="status",
        text="[pipeline step]",
        meta={
            "source": "pipeline",
            "run_id": "run-5731",
            "pipeline_name": "probe",
            "step_index": index,
            "total_steps": 2,
            "step_kind": "transform",
            "step_event": "pipeline_step_started",
        },
    )


@pytest.mark.asyncio
async def test_pipeline_frame_creates_one_flow_entry() -> None:
    """Tier 2: a reached pipeline frame is retained as a visible flow entry."""
    transport = _PipelineTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(80, 24)) as pilot:
        await transport.push(_pipeline_message())
        await pilot.pause()
        await pilot.pause()
        entries = app.query_one(FlowView).entries

    entry = next(
        entry for entry in entries if entry.item.meta.get("run_id") == "run-5731"
    )
    assert entry.item.meta["step_index"] == 0
