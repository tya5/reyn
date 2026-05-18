"""InterventionWidget — inline chip-based ask_user prompt.

Mounts inside ConversationView (not a modal dialog). The user can click
a chip button or type a free-text answer. Either path calls the registered
`answer_callback`.

Design: soft-tinted box with coral border. Chips are Textual Buttons.
Free-text fallback uses a one-line Input that appears when the user
selects "free response".
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label


class InterventionWidget(Widget):
    """Inline intervention prompt with chip buttons.

    Args:
        question:        The prompt text shown to the user.
        choices:         Chip definitions. Each entry is either a legacy
                         ``(label, choice_id)`` 2-tuple or a dict with
                         ``{"label", "id", "hotkey"?, "default"?}``. When the
                         list is empty, only a free-text input is shown.
        answer_callback: Coroutine called with the user's answer string.
        iv_id:           Intervention ID (for logging / targeting).
        queued_extra:    Number of additional pending interventions waiting
                         behind this one. When > 0, a dim ``+N more pending``
                         label is rendered in the widget header so the user
                         has a persistent signal that more questions follow.
    """

    DEFAULT_CSS = """
    InterventionWidget {
        background: #1e1510;
        border: solid $primary;
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
        border: solid $primary;
        color: #ffffff;
        height: 1;
    }
    InterventionWidget Label.iv-hint {
        color: #555555;
        padding-top: 1;
        height: auto;
        width: 1fr;
    }
    InterventionWidget Label.iv-queued {
        color: #888888;
        text-style: dim;
        padding-bottom: 1;
        height: auto;
        width: 1fr;
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
        choices: list[tuple[str, str] | dict] | None = None,
        answer_callback: Callable[[str], Awaitable[None]] | None = None,
        iv_id: str = "",
        queued_extra: int = 0,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id or f"iv_{iv_id[:8]}")
        self._question = question
        # Normalise both legacy 2-tuples and richer dict shapes into a single
        # internal list-of-dicts so compose() / hotkey lookup stays uniform.
        self._choices: list[dict[str, Any]] = [
            self._normalise_choice(c) for c in (choices or [])
        ]
        self._answer_callback = answer_callback
        self._iv_id = iv_id
        self._queued_extra = max(0, int(queued_extra))
        self._show_input = not self._choices  # no chips → always show free text

    @staticmethod
    def _normalise_choice(raw: tuple[str, str] | dict) -> dict[str, Any]:
        """Coerce one choice entry into ``{label, id, hotkey, default}``.

        Accepts the legacy ``(label, id)`` 2-tuple (no hotkey, not default)
        and the richer dict carrying ``hotkey`` / ``default``. Missing keys
        on the dict path fall back to safe blanks so a caller that only
        supplies label + id keeps working.
        """
        if isinstance(raw, dict):
            return {
                "label": raw.get("label", ""),
                "id": raw.get("id", ""),
                "hotkey": raw.get("hotkey") or "",
                "default": bool(raw.get("default", False)),
            }
        # Tuple path — historical shape is (label, id); we ignore extras
        # defensively in case a caller widened it.
        label = raw[0] if len(raw) > 0 else ""
        choice_id = raw[1] if len(raw) > 1 else ""
        return {"label": label, "id": choice_id, "hotkey": "", "default": False}

    @staticmethod
    def _format_chip_label(label: str, hotkey: str) -> str:
        """Render a chip label, avoiding a duplicate ``[h]`` prefix.

        The producer-side factories in ``reyn.intervention_choices`` embed the
        hotkey in the label itself (``"[y]es"``, ``"[A]lways"``, …) so the CLI
        renderer can show the hint without consulting the separate ``hotkey``
        field. When that same choice flows into the TUI chip we don't want to
        double-prefix into ``"[y] [y]es"``. If the label already starts with
        ``[<hotkey>]`` (case-sensitive — hotkeys are case-sensitive), pass it
        through unchanged; otherwise prepend ``[h] `` so callers that supply
        a bare label still get a visible hint.
        """
        if not hotkey:
            return label
        if label.startswith(f"[{hotkey}]"):
            return label
        return f"[{hotkey}] {label}"

    def compose(self) -> ComposeResult:
        yield Label(f"  {self._question}", classes="iv-question")
        if self._queued_extra > 0:
            yield Label(
                f"  +{self._queued_extra} more pending",
                classes="iv-queued",
            )
        if self._choices:
            with Widget(classes="iv-chips"):
                for choice in self._choices:
                    label = choice["label"]
                    hotkey = choice["hotkey"]
                    display = self._format_chip_label(label, hotkey)
                    variant = "primary" if choice["default"] else "default"
                    yield Button(
                        display,
                        id=f"chip_{choice['id']}",
                        variant=variant,
                    )
                # "free response" chip always at end
                yield Button("free response…", id="chip__free", variant="default")
            yield Label(
                "↓ type a free answer in the chat input below, or click a button",
                classes="iv-hint",
            )
        else:
            yield Input(placeholder="type your answer…", id="iv_input")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        btn_id = event.button.id or ""
        if btn_id == "chip__free":
            self._show_free_input()
            return
        # Find the choice whose chip_ prefix matches, then submit its
        # *hotkey* — InterventionRegistry.deliver_answer routes through
        # match_choice() which compares against hotkey, NOT id. Sending
        # the id (e.g. "yes" instead of "y") silently fails the match,
        # leaves iv.future unresolved, and the widget removes itself —
        # the agent then waits forever for an answer the user already
        # gave. Fall back to the id only when the choice has no hotkey
        # (= producer-side anomaly; user gets the "unknown choice" hint
        # instead of a silent deadlock).
        choice_id = btn_id.removeprefix("chip_")
        choice = next(
            (c for c in self._choices if c["id"] == choice_id),
            None,
        )
        if choice is None:
            return
        hotkey = choice["hotkey"]
        await self._submit(hotkey if hotkey else choice_id)

    async def on_key(self, event) -> None:  # textual.events.Key
        """Activate a chip when its hotkey is pressed.

        Skipped while an Input widget has focus — otherwise a user typing a
        free answer that starts with a hotkey letter would be intercepted.
        Match is case-sensitive, mirroring the producer-side ``match_choice``
        contract in ``user_intervention.py``.
        """
        try:
            focused = self.app.focused
        except Exception:
            focused = None
        if isinstance(focused, Input):
            return
        key_char = getattr(event, "character", None) or getattr(event, "key", "")
        if not key_char or len(key_char) != 1:
            return
        for choice in self._choices:
            if choice["hotkey"] and choice["hotkey"] == key_char:
                event.stop()
                # Submit the hotkey (= what match_choice expects) rather
                # than the id — same deadlock concern as the chip-click
                # path. See on_button_pressed for the full rationale.
                await self._submit(choice["hotkey"])
                return

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
        # Restore focus to the input bar. Without an explicit call here
        # Textual's auto-focus-walker picks the next focusable widget in
        # DOM order on removal — often a non-editable widget (a child of
        # ConversationView, or the right panel if it's open), which
        # leaves the user typing into nothing. The input bar is the only
        # widget that the user can keep going from.
        try:
            from .input_bar import InputBar  # late import → avoid cycle
            self.app.query_one("#inputbar", InputBar).focus_input()
        except Exception:
            pass
