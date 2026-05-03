"""InputBar — bottom input area with slash hint footer.

Design decision (recorded per design doc):
  - Uses `Input` widget (not TextArea). TextArea supports multi-line but
    its Shift+Enter newline handling and keybinding interactions were
    inconsistent across Textual 0.50 versions tested. `Input` is simpler
    and the primary use case is single-line prompts. Multi-line pasting
    still works via the paste handler.
  - Footer hints are a single static Label that updates when input is
    empty (shows all commands) or starts with "/" (shows matching commands).

Keybindings (all handled here):
  Enter          → Submit (when input non-empty)
  Tab            → Open slash command palette
  Escape         → Close palette; if none, no-op
  Ctrl+L         → Clear conversation pane (fires ClearConversation message)
  Ctrl+D         → Quit (fires QuitRequested message)
  Up / Down      → Input history when input is empty
  Ctrl+C         → Cancel in-flight task (fires CancelInFlight message)
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label
from textual import on
from textual.binding import Binding


class InputBar(Widget):
    """The bottom input row: Input field + footer hint label + palette."""

    BINDINGS = [
        Binding("ctrl+l", "clear_conversation", "Clear", show=False),
        Binding("ctrl+d", "quit_app", "Quit", show=False),
        Binding("escape", "close_palette", "Close palette", show=False),
    ]

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: auto;
        max-height: 20;
        background: #111111;
        border-top: tall #2a2a2a;
    }
    InputBar Input {
        background: transparent;
        border: none;
        color: #ffffff;
        padding: 0 1;
        height: 1;
    }
    InputBar #hints {
        height: 1;
        color: #555555;
        padding: 0 2;
    }
    InputBar.palette-open #hints {
        display: none;
    }
    """

    # ── messages ──────────────────────────────────────────────────────────────

    class UserSubmitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class ClearConversation(Message):
        pass

    class QuitRequested(Message):
        pass

    class CancelInFlight(Message):
        pass

    class OpenPalette(Message):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix  # current "/" prefix for filtering

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def __init__(
        self,
        *,
        slash_names: list[str] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._slash_names = slash_names or []
        self._history: list[str] = []
        self._history_idx: int = -1  # -1 = not browsing

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message… (Tab: commands, Ctrl+D: quit)", id="input")
        yield Label(self._build_hint(""), id="hints")

    # ── keybinding actions ────────────────────────────────────────────────────

    def action_clear_conversation(self) -> None:
        self.post_message(self.ClearConversation())

    def action_quit_app(self) -> None:
        self.post_message(self.QuitRequested())

    def action_close_palette(self) -> None:
        # Palette is managed in app.py; just focus back to input
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass

    # ── input events ─────────────────────────────────────────────────────────

    @on(Input.Submitted, "#input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        # Add to history
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = -1
        # Clear input
        inp = self.query_one("#input", Input)
        inp.value = ""
        self._update_hint("")
        self.post_message(self.UserSubmitted(text))

    @on(Input.Changed, "#input")
    def on_input_changed(self, event: Input.Changed) -> None:
        self._update_hint(event.value)

    def on_key(self, event) -> None:
        """Handle Tab (palette), Up/Down (history), Ctrl+C (cancel)."""
        inp = self.query_one("#input", Input)
        current = inp.value

        if event.key == "tab":
            event.prevent_default()
            self.post_message(self.OpenPalette(prefix=current))
            return

        if event.key == "ctrl+c":
            event.prevent_default()
            self.post_message(self.CancelInFlight())
            return

        if event.key == "up":
            if not current or self._history_idx > 0:
                event.prevent_default()
                self._history_up(inp)
            return

        if event.key == "down":
            if self._history_idx >= 0:
                event.prevent_default()
                self._history_down(inp)
            return

    # ── history navigation ────────────────────────────────────────────────────

    def _history_up(self, inp: Input) -> None:
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        inp.value = self._history[self._history_idx]
        inp.cursor_position = len(inp.value)

    def _history_down(self, inp: Input) -> None:
        if self._history_idx < 0:
            return
        self._history_idx += 1
        if self._history_idx >= len(self._history):
            self._history_idx = -1
            inp.value = ""
        else:
            inp.value = self._history[self._history_idx]
            inp.cursor_position = len(inp.value)

    # ── hint rendering ────────────────────────────────────────────────────────

    def _build_hint(self, current: str) -> str:
        if not current:
            return "  Ctrl+D quit  │  Ctrl+L clear  │  Ctrl+C cancel  │  Ctrl+B panel  │  Ctrl+O next panel  │  f/t filter/tail (events)"
        return ""

    def _update_hint(self, current: str) -> None:
        try:
            label = self.query_one("#hints", Label)
            label.update(self._build_hint(current))
        except Exception:
            pass

    def update_slash_names(self, names: list[str]) -> None:
        """Update the known slash command names (called after registry loads)."""
        self._slash_names = names
        try:
            inp = self.query_one("#input", Input)
            self._update_hint(inp.value)
        except Exception:
            pass

    def focus_input(self) -> None:
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass
