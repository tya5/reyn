"""#3327 — a keyboard-only user must have a way BACK to the pending
intervention panel after ``Esc``/``Tab`` dismisses it without answering.

Before this fix: ``Esc``/``Tab`` inside the panel (#3299 P1's documented,
INTENDED escape hatch) return focus to the Composer, but nothing anywhere
retargets focus back onto the panel — ``Tab``/``Shift+Tab`` only cycle
Composer↔MenuBar, and the Composer's own ``↓``/``↑`` targeted the menu and
sent-queue, never the panel. A keyboard-only user was stuck permanently.

The fix reuses the package's OWN established idiom (``↑`` on the composer's
first line already reaches upward into the sent-queue when it has items,
#3300 Y-client) rather than inventing a new key: ``↑`` now checks for a
pending intervention FIRST — ``InterventionPanel.has_pending()`` — and, if
one exists, focuses the panel's active tab (``InterventionPanel.focus_pending()``)
instead of the sent-queue. The new behavior is registered in
``chrome.py``'s ``COMPOSER_KEYS`` (the Help pane's single source of truth,
the exact defect class #3314 caught for a previous binding).

Real ``TextualChatApp`` + a real minimal ``ClientTransport`` (mirrors
``test_textual_chat_intervention_panel_3299.py``'s ``RecordingTransport``) —
no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual.widgets import RadioSet, Static

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import COMPOSER_KEYS
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.sent_queue import SentQueue
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class RecordingTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` — replays a fixed frame list,
    then stays open on a live queue so a test can push more frames (e.g. a
    ``user_submitted`` sent-queue event), and RECORDS which answer seam each
    user action reached."""

    def __init__(self, messages: "list[OutboxMessage]") -> None:
        self._initial = list(messages)
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()
        self.submitted: "list[str]" = []
        self.answered_choice: "list[str]" = []
        self.answered_text: "list[str]" = []

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        for msg in self._initial:
            yield DisplayFrame(msg)
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        return "m-" + str(len(self.submitted))

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_text.append(text)
        return True

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        self.answered_choice.append(choice_id)
        return True

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


def _choice_intervention() -> OutboxMessage:
    return OutboxMessage(
        kind="intervention",
        text="Allow write to /etc/hosts?\n  Yes / No",
        meta={
            "intervention_id": "iv-1",
            "intervention_kind": "confirm",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )


def _user_submitted(*, msg_id: str, chain_id: str, text: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": {}},
    )


@pytest.mark.asyncio
async def test_up_from_composer_reaches_panel_after_esc_dismiss_and_answers() -> None:
    """Tier 2b: GATE 1 + GATE 5 — the full keyboard-only recovery sequence.
    ``Esc`` dismisses the panel WITHOUT answering (the escape hatch keeps
    working, gate 5); ``↑`` from the composer then re-focuses the panel
    (gate 1's reachability fix); a normal in-panel keystroke (bare ``Enter``
    on the pre-highlighted first option) answers it.

    NON-VACUITY: focus is checked to be on the Composer (not already on the
    panel) immediately after Esc, before ``↑`` is pressed — so the final
    RadioSet focus cannot be a leftover from before the dismiss."""
    transport = RecordingTransport([_choice_intervention()])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        composer = app.query_one(Composer)
        radio = panel.query_one(RadioSet)
        assert radio.has_focus, "panel did not auto-focus on arrival"

        await pilot.press("escape")
        await pilot.pause()
        assert composer.has_focus, "Esc did not return focus to the Composer"
        assert panel.display is True, "Esc must not answer/collapse the panel"
        assert transport.answered_choice == [], "Esc must not deliver an answer"

        # GATE 1: the keyboard route BACK — ↑ from the composer's (empty,
        # cursor-at-origin) first line.
        await pilot.press("up")
        await pilot.pause()
        assert not composer.has_focus, "↑ did not move focus off the Composer"
        assert radio.has_focus, (
            "↑ did not reach the pending intervention panel's RadioSet — "
            "the #3327 deadlock's reachability gap"
        )

        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert transport.answered_choice == ["yes"], (
            f"the panel, once reached, did not deliver the answer; "
            f"got {transport.answered_choice}"
        )
        assert panel.display is False
        assert composer.has_focus


@pytest.mark.asyncio
async def test_up_prioritizes_pending_panel_over_nonempty_sent_queue() -> None:
    """Tier 2b: with BOTH a pending intervention AND a non-empty sent-queue,
    ``↑`` targets the panel first — answering the intervention is the more
    urgent action (and unblocks the turn that is itself gating the queued
    item's own dispatch)."""
    transport = RecordingTransport([_choice_intervention()])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="queued while busy", seq=1)
        )
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_items(), "test setup: sent-queue must be non-empty"

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        radio = panel.query_one(RadioSet)
        assert radio.has_focus, (
            "↑ focused the sent-queue instead of the higher-priority pending "
            "intervention panel"
        )
        assert not sent_queue.has_focus


@pytest.mark.asyncio
async def test_up_still_reaches_sent_queue_when_nothing_pending() -> None:
    """Tier 2b: regression guard — with NO pending intervention, ``↑`` keeps
    its pre-#3327 behavior (focus the non-empty sent-queue) unchanged."""
    transport = RecordingTransport([])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            _user_submitted(msg_id="m1", chain_id="c1", text="queued", seq=1)
        )
        await pilot.pause()

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        sent_queue = app.query_one(SentQueue)
        assert sent_queue.has_focus, (
            "↑ did not focus the sent-queue when nothing is pending — "
            "the pre-#3327 behavior regressed"
        )


@pytest.mark.asyncio
async def test_help_pane_renders_the_new_up_binding_description() -> None:
    """Tier 2b: GATE 4 — the new ``↑`` behavior is DISCOVERABLE through the
    ACTUAL RENDERED Help pane widget (never just the ``COMPOSER_KEYS``
    constant — the #3314 co-vet finding this exact check pattern guards
    against: an existence check on the constant would pass even if the pane
    never rendered it)."""
    transport = RecordingTransport([])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        rendered = str(app.query_one("#help", Static).render())
        assert "focus pending intervention" in rendered, (
            f"the new ↑ binding description is missing from the RENDERED "
            f"Help pane; got: {rendered!r}"
        )

    # Non-vacuity: the constant itself carries the exact text asserted above
    # (proves the assertion isn't matching on unrelated boilerplate).
    assert any("focus pending intervention" in desc for _key, desc in COMPOSER_KEYS)
