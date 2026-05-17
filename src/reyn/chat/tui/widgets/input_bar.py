"""InputBar — bottom input area with inline slash-command picker.

Design decision:
  - `TextArea` for multi-line input. Enter submits; Ctrl+J inserts a newline.
  - SlashPicker (Discord/Slack-style) auto-shows when the input starts with
    "/" and is filtered live as the user types. Focus stays on the TextArea
    at all times — the picker is a passive renderer driven from here.

Keybindings (handled here):
  Enter         → If picker has matches: splice in the highlighted command
                  ("/cmdname") and submit in one keypress.
                  Otherwise: submit the message as typed.
  Ctrl+J        → Insert a newline.
  Ctrl+U        → Clear the whole input (single or multi-line).
  Tab           → Confirm-without-submit: insert "/cmdname " and keep
                  focus so the user can type args. No-op when picker
                  is closed.
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
        Binding("escape", "dismiss_picker", "Dismiss", priority=True, show=False),
        Binding("ctrl+j", "newline", "Newline", priority=True, show=False),
        Binding("ctrl+u", "clear_input", "Clear input", priority=True, show=False),
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
        border-top: solid #2a2a2a;
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

    def append_text(self, text: str) -> None:
        """Append text to the current input (used by voice dictation).

        Adds a single space separator if existing text doesn't already end
        with whitespace, so successive F2 sessions concatenate naturally.
        """
        if not text:
            return
        try:
            ta = self.query_one("#input", TextArea)
        except Exception:
            return
        existing = ta.text
        sep = "" if (not existing or existing[-1].isspace()) else " "
        new_text = existing + sep + text
        ta.load_text(new_text)
        # Move cursor to end so Enter sends or user can continue typing
        lines = new_text.split("\n")
        last_row = len(lines) - 1
        ta.move_cursor((last_row, len(lines[last_row])))

    # ── input events ─────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#input")
    def on_textarea_changed(self, event: TextArea.Changed) -> None:
        self._update_picker(event.text_area.text)

    # ── action handlers (priority-bound keys) ────────────────────────────────

    def action_submit_or_confirm(self) -> None:
        """Enter — submit the message, expanding the picker selection in
        one keypress when the picker is open.

        Previously Enter only confirmed (inserted) the picker selection, so
        the user had to press Enter twice to actually send a slash command.
        Now: with the picker open we splice in the highlighted match and
        submit immediately. Tab still does insert-without-submit, so users
        who want to type args after the command name can use it.
        """
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            cmd = picker.selected_command()
            if cmd is not None:
                ta.load_text(f"/{cmd.name}")
                picker.hide()
                self._submit(ta)
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
        """Escape — abandon slash entry: hide picker AND clear the prefix.

        The picker is only visible when the input is ``/<name-partial>``
        with no space or newline (see ``_update_picker``), so the entire
        text is the slash prefix being discovered. Leaving it behind made
        Esc feel like a no-op and forced the user to backspace before
        typing anything else — Slack/Discord clear the prefix on Esc.
        """
        self.dismiss_slash_prefix()

    def dismiss_slash_prefix(self) -> bool:
        """Hide the picker and clear the slash prefix it was tracking.

        Returns True if the picker was actually visible (and got dismissed),
        False otherwise. The App's ``action_cancel_inflight`` calls this
        as an early branch on Ctrl+C so an open picker dismisses instead
        of producing a misleading "nothing in-flight" message.
        """
        picker = self._picker()
        if picker is None or not picker.visible_:
            return False
        picker.hide()
        ta = self._textarea()
        if ta is not None:
            ta.load_text("")
        return True

    def action_newline(self) -> None:
        ta = self._textarea()
        if ta is not None:
            ta.insert("\n")

    def action_clear_input(self) -> None:
        """Ctrl+U — wipe the whole input.

        The TextArea's default Ctrl+U only deletes from the cursor back
        to the start of the current line, which is unintuitive for a
        multi-line composer. Operators expect Ctrl+U to clear the entire
        buffer (matching readline-on-a-single-line semantics for the
        common case).
        """
        ta = self._textarea()
        if ta is not None:
            ta.clear()
        picker = self._picker()
        if picker is not None and picker.visible_:
            picker.hide()

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

    def on_slash_picker_clicked(self, event: SlashPicker.Clicked) -> None:
        """Click on a picker row — insert the highlighted command.

        The picker's ``on_click`` already moved its own ``_selected`` to
        the clicked row before posting the message, so this just routes
        the existing confirm path. Mirrors the Tab key flow exactly:
        ``/<name> `` lands in the TextArea with the cursor past the
        trailing space, and the picker hides itself.
        """
        picker = self._picker()
        ta = self._textarea()
        if picker is not None and ta is not None:
            self._confirm_picker(picker, ta)
            # Re-focus the TextArea — the click may have implicitly
            # shifted focus context even though SlashPicker is
            # ``can_focus = False``, and the user expects to keep typing.
            ta.focus()

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
        # Fits in 80 cols (= 72 cells incl. 2-space left margin) so the
        # tail "Ctrl+P/N turn" doesn't get clipped on default terminals.
        # Dropped Ctrl+J nl in addition to Ctrl+O / Ctrl+R / Ctrl+\ /
        # Ctrl+C — all remain discoverable via the Keys tab (Ctrl+B).
        return (
            "  Enter send │ Ctrl+D quit │ Ctrl+L clear │ "
            "Ctrl+B panel │ Ctrl+P/N turn"
        )
