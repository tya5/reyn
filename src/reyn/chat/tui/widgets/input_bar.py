"""InputBar — bottom input area with slash hint footer.

Design decision:
  - Uses `TextArea` for multi-line input. Enter submits; Shift+Enter inserts
    a newline. This matches standard chat UX (Slack / Discord style).
  - Footer hints update when input is empty or starts with "/".

Keybindings (all handled here):
  Enter          → Submit (when input non-empty)
  Shift+Enter    → Insert newline
  Tab            → Open slash command palette
  Escape         → Close palette; if none, no-op
  Ctrl+L         → Clear conversation pane (fires ClearConversation message)
  Ctrl+D         → Quit (fires QuitRequested message)
  Up             → Input history (only when cursor is on the first row)
  Down           → Input history (only when cursor is on the last row)
  Ctrl+C         → Cancel in-flight task (fires CancelInFlight message)
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import TextArea, Label
from textual import on
from textual.binding import Binding


class InputBar(Widget):
    """The bottom input row: TextArea + footer hint label + palette."""

    BINDINGS = [
        Binding("ctrl+l", "clear_conversation", "Clear", show=False),
        Binding("ctrl+d", "quit_app", "Quit", show=False),
        Binding("escape", "close_palette", "Close palette", show=False),
        Binding("enter", "submit", "Submit", show=False, priority=True),
        Binding("ctrl+j", "newline", "Newline", show=False, priority=True),
        Binding("tab", "open_palette", "Commands", show=False, priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: auto;
        max-height: 20;
        background: #111111;
        border-top: tall #2a2a2a;
    }
    InputBar TextArea {
        background: transparent;
        border: none;
        color: #ffffff;
        padding: 0 1;
        height: auto;
        max-height: 10;
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
            self.prefix = prefix

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
        self._history_idx: int = -1

    def compose(self) -> ComposeResult:
        yield TextArea(
            id="input",
            language=None,
            show_line_numbers=False,
            soft_wrap=True,
        )
        yield Label(self._build_hint(""), id="hints")

    def on_mount(self) -> None:
        try:
            ta = self.query_one("#input", TextArea)
            # Disable the line-number gutter and syntax highlighting chrome.
            ta.show_line_numbers = False
        except Exception:
            pass

    # ── keybinding actions ────────────────────────────────────────────────────

    def action_clear_conversation(self) -> None:
        self.post_message(self.ClearConversation())

    def action_quit_app(self) -> None:
        self.post_message(self.QuitRequested())

    def action_close_palette(self) -> None:
        try:
            self.query_one("#input", TextArea).focus()
        except Exception:
            pass

    def action_submit(self) -> None:
        try:
            ta = self.query_one("#input", TextArea)
        except Exception:
            return
        text = ta.text.strip()
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = -1
        ta.clear()
        self._update_hint("")
        self.post_message(self.UserSubmitted(text))

    def action_newline(self) -> None:
        try:
            self.query_one("#input", TextArea).insert("\n")
        except Exception:
            pass

    def action_open_palette(self) -> None:
        try:
            ta = self.query_one("#input", TextArea)
            prefix = ta.text
        except Exception:
            prefix = ""
        self.post_message(self.OpenPalette(prefix=prefix))

    def action_cancel(self) -> None:
        self.post_message(self.CancelInFlight())

    # ── input events ─────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#input")
    def on_textarea_changed(self, event: TextArea.Changed) -> None:
        self._update_hint(event.text_area.text)

    def on_key(self, event) -> None:
        """Up/Down history — only when cursor is at the edge row."""
        if event.key not in ("up", "down"):
            return
        try:
            ta = self.query_one("#input", TextArea)
        except Exception:
            return

        row, _ = ta.cursor_location

        if event.key == "up" and row == 0:
            event.prevent_default()
            self._history_up(ta)
        elif event.key == "down":
            last_row = ta.text.count("\n")
            if row >= last_row:
                event.prevent_default()
                self._history_down(ta)

    # ── history navigation ────────────────────────────────────────────────────

    def _history_up(self, ta: TextArea) -> None:
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._load_history_entry(ta, self._history[self._history_idx])

    def _history_down(self, ta: TextArea) -> None:
        if self._history_idx < 0:
            return
        self._history_idx += 1
        if self._history_idx >= len(self._history):
            self._history_idx = -1
            ta.clear()
        else:
            self._load_history_entry(ta, self._history[self._history_idx])

    def _load_history_entry(self, ta: TextArea, text: str) -> None:
        ta.load_text(text)
        lines = text.split("\n")
        last_row = len(lines) - 1
        ta.move_cursor((last_row, len(lines[last_row])))

    # ── hint rendering ────────────────────────────────────────────────────────

    def _build_hint(self, current: str) -> str:
        return "  Enter send  │  Ctrl+J newline  │  Ctrl+D quit  │  Ctrl+L clear  │  Ctrl+C cancel  │  Ctrl+B panel  │  Ctrl+O focus panel  │  Ctrl+\\ shot"

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
            ta = self.query_one("#input", TextArea)
            self._update_hint(ta.text)
        except Exception:
            pass

    def focus_input(self) -> None:
        try:
            self.query_one("#input", TextArea).focus()
        except Exception:
            pass
