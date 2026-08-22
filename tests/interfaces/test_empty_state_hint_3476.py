"""#3476 ② — the conversation pane's empty-state hint.

A fresh session previously opened onto a blank void above the composer
(owner design review). flowview 0.6.0's ``empty=`` shows a hint across the
viewport while the model has no entries and clears it the moment the first
entry lands. These tests read the PAINTED surface (``FlowView.render_line``,
the same rows a terminal receives) rather than a private attribute, so they
pin what a user sees, not what was configured.

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` — no mocks."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

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

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _painted_text(flow: FlowView) -> str:
    """Every visible row of the conversation pane, as painted."""
    return "\n".join(
        "".join(segment.text for segment in flow.render_line(y))
        for y in range(flow.size.height)
    )


@pytest.mark.asyncio
async def test_fresh_session_paints_the_welcome_hint() -> None:
    """Tier 2b: a fresh session (no history, nothing pumped) paints the hint
    where the blank void used to be — read from the rendered rows."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        painted = _painted_text(app.query_one(FlowView))
        assert "Type a message to start" in painted, (
            f"the empty-state hint is not painted; pane shows: {painted!r}"
        )
        assert "Help tab for keys" in painted


@pytest.mark.asyncio
async def test_hint_clears_when_the_first_entry_lands() -> None:
    """Tier 2b: the hint is the EMPTY state, not a banner — the first real
    entry replaces it (flowview owns the transition; this pins that reyn's
    wiring lets it happen rather than pinning the hint over content)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="status", text="first row"))
        await pilot.pause()
        painted = _painted_text(app.query_one(FlowView))
        assert "Type a message to start" not in painted, (
            "the empty-state hint is still painted after content arrived"
        )
        assert "first row" in painted
