"""#3621 — the terminal pen ends each frame where the composer's cursor is.

Textual returns the pen to ``app.cursor_position`` after every render, so that
offset is also where an IME anchors its candidate window. Upstream keeps it as a
STORED value refreshed only by ``TextArea``'s own events — gaining focus, and
moving the cursor — while the value itself, ``cursor_screen_offset``, is derived
from the widget's ``content_region``. So it goes stale whenever the composer is
laid out somewhere else with the cursor sitting still, which is what a growing
conversation does, on repaints rather than on keystrokes.

Stale here does not mean "slightly behind". It means the pen is sent to whatever
now occupies those rows: measured on a 30-row terminal, two rows below the real
cursor is the MENU BAR, and the owner watched the IME candidate window jump
there and back without typing.

These assert the RELATION (published == real) rather than any particular row, so
they keep meaning if the chrome is ever laid out differently.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

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

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_the_anchor_follows_a_conversation_that_grows() -> None:
    """Tier 2b: appending replies moves the composer, and the anchor moves with it.

    This is the owner's case: nothing is typed, the pane repaints, and upstream's
    stored offset would still name the composer's OLD rows.
    """
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        for i in range(6):
            app.conversation.append(
                OutboxMessage(kind="agent", text=f"reply {i} " * 20)
            )
        await pilot.pause()

        assert app.cursor_position == composer.cursor_screen_offset, (
            "the pen is returned to where the composer USED to be — an IME "
            "anchors its candidate window there"
        )


@pytest.mark.asyncio
async def test_the_anchor_follows_the_box_growing_under_the_cursor() -> None:
    """Tier 2b: the composer auto-growing also moves its own cursor."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        composer.text = "one\ntwo\nthree"
        await pilot.pause()

        assert app.cursor_position == composer.cursor_screen_offset


@pytest.mark.asyncio
async def test_a_widget_that_is_not_the_composer_keeps_the_stored_anchor() -> None:
    """Tier 2b: the derivation is scoped to the box the user types in.

    Reporting the composer's cursor while focus is elsewhere would anchor the
    IME to a box that is not receiving the keystrokes, so the stored value has
    to remain the answer for every other widget — including none focused.
    """
    from textual.geometry import Offset

    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#menubar").focus()
        await pilot.pause()

        sentinel = Offset(7, 9)
        app.cursor_position = sentinel

        assert app.cursor_position == sentinel, (
            "a non-composer focus no longer round-trips the stored offset"
        )
