"""InputBar — bottom input area with inline slash-command picker.

Design decision:
  - `TextArea` for multi-line input. Enter submits; Ctrl+J inserts a newline.
  - SlashPicker (Discord/Slack-style) auto-shows when the input starts with
    "/" and is filtered live as the user types. Focus stays on the TextArea
    at all times — the picker is a passive renderer driven from here.

Keybindings (handled here):
  Enter         → If picker has matches: insert "/cmdname " (confirm).
                  Otherwise: submit the message.
  Ctrl+J        → Insert a newline.
  Tab           → Same as confirm-from-picker (no-op when picker is closed).
  Up / Down     → Picker selection when picker visible; otherwise input
                  history when the cursor is on the first/last row.
  Escape        → Hide picker (keep the typed text).
  Ctrl+L        → Clear conversation pane (fires ClearConversation).
  Ctrl+D        → Quit (fires QuitRequested).
  Ctrl+C        → Cancel in-flight task (fires CancelInFlight).
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, TextArea

from reyn.chat.slash import SlashCommand

from .slash_picker import SlashPicker


class InputBar(Widget):
    """The bottom input row: SlashPicker + TextArea + footer hint."""

    BINDINGS = [
        # All priority bindings — they fire before the TextArea consumes the key.
        Binding("enter", "submit_or_confirm", "Submit", priority=True, show=False),
        Binding("tab", "confirm_picker", "Confirm", priority=True, show=False),
        Binding("up", "key_up", "Up", priority=True, show=False),
        Binding("down", "key_down", "Down", priority=True, show=False),
        Binding("escape", "dismiss_picker", "Esc", priority=True, show=False),
        Binding("ctrl+j", "newline", "Newline", priority=True, show=False),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True, show=False),
        Binding("ctrl+d", "quit_app", "Quit", priority=True, show=False),
        Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
    ]

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: auto;
        max-height: 22;
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

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def __init__(
        self,
        *,
        slash_commands: list[SlashCommand] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._slash_commands: list[SlashCommand] = list(slash_commands or [])
        self._history: list[str] = []
        self._history_idx: int = -1

    def compose(self) -> ComposeResult:
        # Picker docked above TextArea (compose order matters: top-down).
        yield SlashPicker(id="slash-picker")
        yield TextArea(
            id="input",
            language=None,
            show_line_numbers=False,
            soft_wrap=True,
        )
        yield Label(self._build_hint(), id="hints")

    def on_mount(self) -> None:
        try:
            ta = self.query_one("#input", TextArea)
            ta.show_line_numbers = False
        except Exception:
            pass

    # ── public API ────────────────────────────────────────────────────────────

    def update_slash_commands(self, commands: list[SlashCommand]) -> None:
        """Receive the full SlashCommand list from the registry."""
        self._slash_commands = [c for c in commands if not c.hidden]
        # If picker is currently visible, refresh it
        try:
            ta = self.query_one("#input", TextArea)
            self._update_picker(ta.text)
        except Exception:
            pass

    def focus_input(self) -> None:
        try:
            self.query_one("#input", TextArea).focus()
        except Exception:
            pass

    # ── input events ─────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#input")
    def on_textarea_changed(self, event: TextArea.Changed) -> None:
        self._update_picker(event.text_area.text)

    # ── action handlers (priority-bound keys) ────────────────────────────────

    def action_submit_or_confirm(self) -> None:
        """Enter — confirm picker if visible, else submit the message."""
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            self._confirm_picker(picker, ta)
            return
        self._submit(ta)

    def action_confirm_picker(self) -> None:
        """Tab — confirm picker selection. No-op when picker is closed."""
        picker = self._picker()
        ta = self._textarea()
        if picker is not None and picker.visible_ and picker.has_matches and ta is not None:
            self._confirm_picker(picker, ta)

    def action_key_up(self) -> None:
        """Up — picker selection if open, else input history at top edge."""
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            picker.move_selection(-1)
            return
        row, _ = ta.cursor_location
        if row == 0:
            self._history_up(ta)
            return
        # Multi-line: move cursor up within TextArea
        ta.action_cursor_up()

    def action_key_down(self) -> None:
        """Down — picker selection if open, else input history at bottom edge."""
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            picker.move_selection(+1)
            return
        last_row = ta.text.count("\n")
        row, _ = ta.cursor_location
        if row >= last_row:
            self._history_down(ta)
            return
        ta.action_cursor_down()

    def action_dismiss_picker(self) -> None:
        """Escape — hide picker but keep typed text."""
        picker = self._picker()
        if picker is not None and picker.visible_:
            picker.hide()

    def action_newline(self) -> None:
        ta = self._textarea()
        if ta is not None:
            ta.insert("\n")

    def action_clear_conversation(self) -> None:
        self.post_message(self.ClearConversation())

    def action_quit_app(self) -> None:
        self.post_message(self.QuitRequested())

    def action_cancel(self) -> None:
        self.post_message(self.CancelInFlight())

    # ── widget accessors ─────────────────────────────────────────────────────

    def _picker(self) -> SlashPicker | None:
        try:
            return self.query_one("#slash-picker", SlashPicker)
        except Exception:
            return None

    def _textarea(self) -> TextArea | None:
        try:
            return self.query_one("#input", TextArea)
        except Exception:
            return None

    # ── picker logic ─────────────────────────────────────────────────────────

    def _update_picker(self, text: str) -> None:
        try:
            picker = self.query_one("#slash-picker", SlashPicker)
        except Exception:
            return
        # Picker shows only when input starts with "/" and contains no newline
        # (multi-line input is not a command).
        if not text.startswith("/") or "\n" in text:
            picker.hide()
            return
        token = text[1:]
        # Stop matching once the user types a space (entering args)
        if " " in token:
            picker.hide()
            return
        matches = [
            c for c in self._slash_commands
            if c.name.startswith(token)
        ]
        # Sort by name length then alpha so closer prefix shows first
        matches.sort(key=lambda c: (len(c.name), c.name))
        picker.set_matches(matches)

    def _confirm_picker(self, picker: SlashPicker, ta: TextArea) -> None:
        cmd = picker.selected_command()
        if cmd is None:
            return
        new_text = f"/{cmd.name} "
        ta.load_text(new_text)
        ta.move_cursor((0, len(new_text)))
        picker.hide()

    # ── submit / history ─────────────────────────────────────────────────────

    def _submit(self, ta: TextArea) -> None:
        text = ta.text.strip()
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = -1
        ta.clear()
        # Hide picker on submit (in case it was somehow visible)
        try:
            self.query_one("#slash-picker", SlashPicker).hide()
        except Exception:
            pass
        self.post_message(self.UserSubmitted(text))

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

    def _build_hint(self) -> str:
        return (
            "  Enter send  │  Ctrl+J newline  │  Ctrl+D quit  │  "
            "Ctrl+L clear  │  Ctrl+C cancel  │  Ctrl+B panel  │  "
            "Ctrl+O focus panel  │  Ctrl+\\ shot"
        )
