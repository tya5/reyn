"""InterventionWidget — inline chip-based ask_user prompt.

Mounts inside ConversationView (not a modal dialog). The user can click
a chip button or type a free-text answer. Either path calls the registered
`answer_callback`.

Design: soft-tinted box with coral border. Chips are Textual Buttons.
Free-text fallback uses a one-line Input that appears when the user
selects "free response".
"""
from __future__ import annotations

from typing import Awaitable, Callable

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label


class InterventionWidget(Widget):
    """Inline intervention prompt with chip buttons.

    Args:
        question:        The prompt text shown to the user.
        choices:         List of (label, choice_id) tuples for chip buttons.
                         When empty, only a free-text input is shown.
        answer_callback: Coroutine called with the user's answer string.
        iv_id:           Intervention ID (for logging / targeting).
    """

    DEFAULT_CSS = """
    InterventionWidget {
        background: #1e1510;
        border: tall $primary;
        padding: 1 2;
        height: auto;
        margin: 1 0;
        color: #eeddcc;
    }
    InterventionWidget Label.iv-question {
        color: #ffcc88;
        text-style: bold;
        padding-bottom: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget .iv-chips {
        layout: horizontal;
        height: auto;
        margin-top: 0;
    }
    InterventionWidget Button {
        margin: 0 1 0 0;
        background: $primary;
        color: #ffffff;
        border: none;
        padding: 0 2;
        height: 1;
        min-width: 10;
    }
    InterventionWidget Button:hover {
        background: #e0664e;
    }
    InterventionWidget Input {
        margin-top: 1;
        background: #2a1a10;
        border: tall $primary;
        color: #ffffff;
        height: 1;
    }
    """

    class Answered(Message):
        """Posted when the user answers the intervention."""
        def __init__(self, iv_id: str, answer: str) -> None:
            super().__init__()
            self.iv_id = iv_id
            self.answer = answer

    def __init__(
        self,
        *,
        question: str,
        choices: list[tuple[str, str]] | None = None,
        answer_callback: Callable[[str], Awaitable[None]] | None = None,
        iv_id: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id or f"iv_{iv_id[:8]}")
        self._question = question
        self._choices = choices or []
        self._answer_callback = answer_callback
        self._iv_id = iv_id
        self._show_input = not self._choices  # no chips → always show free text

    def compose(self) -> ComposeResult:
        yield Label(f"  {self._question}", classes="iv-question")
        if self._choices:
            with Widget(classes="iv-chips"):
                for label, choice_id in self._choices:
                    yield Button(label, id=f"chip_{choice_id}", variant="default")
                # "free response" chip always at end
                yield Button("free response…", id="chip__free", variant="default")
        else:
            yield Input(placeholder="type your answer…", id="iv_input")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        btn_id = event.button.id or ""
        if btn_id == "chip__free":
            self._show_free_input()
            return
        # Find the choice whose chip_ prefix matches
        choice_id = btn_id.removeprefix("chip_")
        await self._submit(choice_id)

    def _show_free_input(self) -> None:
        """Show a text input for free-form answer."""
        # Try to mount an Input after the chips area
        try:
            existing = self.query_one("#iv_input")
            existing.focus()
            return
        except Exception:
            pass
        inp = Input(placeholder="type your answer…", id="iv_input")
        self.mount(inp)
        inp.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        await self._submit(event.value)

    async def _submit(self, answer: str) -> None:
        """Deliver the answer and post Answered message."""
        if self._answer_callback is not None:
            try:
                await self._answer_callback(answer)
            except Exception:
                pass
        self.post_message(self.Answered(iv_id=self._iv_id, answer=answer))
        # Remove ourselves from the conversation view
        self.remove()
