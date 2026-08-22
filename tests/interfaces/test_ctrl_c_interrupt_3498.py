"""#3498 — ctrl+c interrupts the in-flight turn.

`ClientTransport.cancel_inflight`'s own contract calls itself "the ctrl-c
seam", but no client ever called it: the TUI bound nothing to the key, and two
bindings ahead of the app claimed it anyway — `TextArea` binds
`ctrl+c,super+c` to `copy` (and the Composer holds focus almost always), with
Textual's `App.BINDINGS` binding `ctrl+c` to `help_quit` behind that. So the
owner saw "Press ctrl+q to quit the app" instead of an interrupt.

These tests pin the wiring END TO END through a real keypress on a real
`TextualChatApp`, because that is the only way the two competing bindings are
actually in play — calling `action_cancel_turn()` directly would pass even if
the key never reached it, which is exactly the hole this issue was. What is
asserted is the transport's own record of being asked to cancel; the transport
is a real minimal `ClientTransport`, not a mock.
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
    """A real minimal transport that RECORDS cancel requests (its own public
    surface is what the tests read — no private app state)."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancels = 0

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

    async def cancel_inflight(self) -> None:
        self.cancels += 1

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


class _RaisingTransport(_Transport):
    async def cancel_inflight(self) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_ctrl_c_from_the_composer_reaches_cancel_inflight() -> None:
    """Tier 2b: ctrl+c pressed with the COMPOSER focused — the app's resting
    state, and the case that used to lose the key to ``TextArea``'s own
    ``ctrl+c`` copy binding — reaches the transport's cancel seam."""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        assert composer.has_focus, "setup: the composer does not hold focus"

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert transport.cancels == 1, (
            "ctrl+c did not reach cancel_inflight from the composer — the key "
            "is being consumed ahead of the app binding"
        )


@pytest.mark.asyncio
async def test_ctrl_c_does_not_quit_the_app() -> None:
    """Tier 2b: ctrl+c INTERRUPTS, it does not exit — Textual's default binds
    this key to ``help_quit``, and taking it over must not leave the app
    running-but-unusable or tear it down. (``ctrl+q`` keeps owning quit.)"""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running, "ctrl+c exited the app instead of interrupting"
        assert transport.cancels == 1

        # Still usable afterwards: a second interrupt still lands.
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert transport.cancels == 2, "the app stopped responding to ctrl+c"


@pytest.mark.asyncio
async def test_ctrl_c_interrupts_from_the_conversation_pane_too() -> None:
    """Tier 2b: the interrupt is not composer-only — it works from another
    focus state as well, so a user who stepped into the conversation pane
    (``Shift+Tab``) is not stranded without a way to stop a running turn."""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply"))
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert not app.query_one(Composer).has_focus, (
            "setup: focus never left the composer"
        )

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert transport.cancels == 1


@pytest.mark.asyncio
async def test_a_failing_interrupt_is_surfaced_not_raised() -> None:
    """Tier 2b: if the cancel seam raises, the user is TOLD — an interrupt that
    fails silently would leave them believing a turn was stopped. The error
    reaches the conversation as a frame rather than escaping the handler."""
    app = TextualChatApp(transport=_RaisingTransport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        texts = [entry.item.text for entry in app.conversation]
        assert any("interrupt failed" in text for text in texts), (
            f"a raising cancel_inflight was swallowed; pane holds: {texts!r}"
        )
        assert app.is_running, "a failing interrupt tore the app down"


@pytest.mark.asyncio
async def test_ctrl_c_interrupts_even_with_a_composer_selection() -> None:
    """Tier 2b: ctrl+c interrupts even while text is SELECTED in the composer.

    This is the case that actually needs the binding's ``priority`` flag, and
    the reason the other tests here are not sufficient on their own:
    ``TextArea``'s ``ctrl+c`` copy action is disabled while nothing is
    selected, so with an empty selection the key reaches the app whether or not
    the binding is priority. With a selection the focused widget consumes it —
    a non-priority binding never fires (measured). Falsified: dropping
    ``priority=True`` turns THIS test red and leaves the rest green, which is
    how a plain binding could otherwise ship looking fully covered.

    It is also the owner's decision made observable: ctrl+c means interrupt
    unconditionally, so having text selected must not silently turn it back
    into a copy."""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.text = "some text the user selected"
        composer.selection = ((0, 0), (0, 9))
        await pilot.pause()
        assert composer.selected_text, "setup: nothing is selected in the composer"
        assert composer.check_action("copy", ()) is True, (
            "setup: TextArea's copy action is not enabled, so this test would "
            "not exercise the competing binding at all"
        )

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert transport.cancels == 1, (
            "ctrl+c was consumed by the composer's copy binding instead of "
            "interrupting — the app binding needs priority=True"
        )


def test_the_help_pane_lists_the_interrupt_key() -> None:
    """Tier 2: ctrl+c reaches the HELP pane, not just the key handler.

    The app's Help readout is built from the binding table itself so it cannot
    drift from the keymap — but the reader only understood 3-tuples, and a
    binding carrying a flag (``priority=True``) is a ``Binding`` object. That
    shape was silently skipped: the key worked while the Help pane never
    mentioned it. Nothing raised, so only an assertion on the RENDERED help
    catches it."""
    from reyn.interfaces.inline.textual_chat.chrome import help_pane_lines

    app = TextualChatApp.__new__(TextualChatApp)
    pairs = TextualChatApp._app_binding_help(app)
    lines = "\n".join(help_pane_lines(app_bindings=pairs))
    assert "ctrl+c" in lines, (
        f"the interrupt key is missing from the Help pane; it lists: {lines!r}"
    )
    assert "Interrupt" in lines, "ctrl+c is listed without saying what it does"
