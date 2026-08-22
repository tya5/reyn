"""#3470 — PageUp/PageDown scroll the conversation from the composer.

Owner design review found that "scroll back through the chat" — a chat UI's
basic read-back action — had NO discoverable key: composer-focused PageUp was
swallowed by TextArea's own page-cursor binding (a no-op in a <= 6-row box),
and the only working route was an undocumented Shift+Tab focus hop into the
conversation pane with no visual cue that focus had left the composer.

The fix delegates PageUp/PageDown from ``Composer._on_key`` to the FlowView's
page-scroll — unconditionally (one meaning per key, #3365 principle), with
focus never leaving the composer — and registers the keys in
``COMPOSER_KEYS`` so the Help pane (the #3314 single source of truth)
documents them.

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` — no mocks."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import COMPOSER_KEYS, help_pane_lines
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


async def _overflowing_conversation(app: TextualChatApp, pilot) -> FlowView:
    """Append enough rows that the conversation genuinely overflows the
    viewport (asserted, so the scroll assertions below cannot pass vacuously
    on an unscrollable pane)."""
    for i in range(60):
        app.conversation.append(OutboxMessage(kind="status", text=f"row {i}"))
    await pilot.pause()
    await pilot.pause()
    flow = app.query_one(FlowView)
    assert flow.max_scroll_y > 0, (
        "test setup: the conversation does not overflow the viewport"
    )
    return flow


@pytest.mark.asyncio
async def test_pageup_scrolls_conversation_and_focus_stays_on_composer() -> None:
    """Tier 2b: composer-focused PageUp scrolls the conversation BACK (the
    #3470 fix) and focus never leaves the composer — the delegation is a
    scroll, not a focus hop."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        flow = await _overflowing_conversation(app, pilot)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        at_bottom = flow.scroll_y
        await pilot.press("pageup")
        await pilot.pause()

        assert flow.scroll_y < at_bottom, (
            f"PageUp did not scroll the conversation (scroll_y stayed at "
            f"{flow.scroll_y})"
        )
        assert composer.has_focus, "PageUp moved focus off the composer"


@pytest.mark.asyncio
async def test_pagedown_scrolls_back_toward_the_bottom() -> None:
    """Tier 2b: PageDown reverses PageUp — the user can return to the live
    tail without any focus change."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        flow = await _overflowing_conversation(app, pilot)
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("pageup")
        await pilot.press("pageup")
        await pilot.pause()
        scrolled_up = flow.scroll_y

        await pilot.press("pagedown")
        await pilot.pause()

        assert flow.scroll_y > scrolled_up, (
            f"PageDown did not scroll back down (scroll_y stayed at "
            f"{flow.scroll_y})"
        )
        assert composer.has_focus, "PageDown moved focus off the composer"


def test_scroll_keys_are_discoverable_through_the_help_pane() -> None:
    """Tier 2b: the new keys ride ``COMPOSER_KEYS`` (the Help pane's #3314
    single source of truth) — a key that works but is not documented there is
    exactly the defect #3470 was filed about."""
    assert any(
        "pgup" in key and "scroll" in desc for key, desc in COMPOSER_KEYS
    ), "pgup/pgdn scroll keys missing from COMPOSER_KEYS"
    lines = help_pane_lines()
    assert any("scroll conversation" in line for line in lines), (
        "the scroll keys did not reach the rendered Help pane"
    )
