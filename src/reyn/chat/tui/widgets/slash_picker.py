"""SlashPicker — Discord/Slack-style inline slash-command suggestion list.

Design contract:
  - Non-focusable. Focus stays on the TextArea above it at all times.
  - Auto-shows when the current input starts with "/" and has ≥ 1 match.
  - Filters as the user types (prefix match against command name).
  - ↑/↓ navigates highlighted row, Tab confirms, Esc dismisses.
  - Confirm replaces the entire TextArea content with "/cmdname " (no
    trailing space stripping; cursor lands at end so user can type args).

The picker is intentionally a passive renderer: InputBar drives state
(matches + selected index) and intercepts navigation keys. The picker
just exposes set_matches() / move_selection() / selected_command() and
re-renders itself.
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from reyn.chat.slash import SlashCommand


_CORAL = "#C8553D"
_MAX_VISIBLE = 8


class SlashPicker(Static):
    """Inline list of slash-command matches shown above the TextArea."""

    DEFAULT_CSS = """
    SlashPicker {
        height: auto;
        max-height: 10;
        background: #1a1a1a;
        border: tall $primary;
        padding: 0 1;
        display: none;
    }
    SlashPicker.visible {
        display: block;
    }
    """

    can_focus = False

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._matches: list[SlashCommand] = []
        self._selected: int = 0

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def visible_(self) -> bool:
        return self.has_class("visible")

    @property
    def has_matches(self) -> bool:
        return bool(self._matches)

    def set_matches(self, matches: list[SlashCommand]) -> None:
        """Replace the match list. Resets selection to row 0."""
        self._matches = list(matches)[:_MAX_VISIBLE]
        self._selected = 0
        if self._matches:
            self.add_class("visible")
        else:
            self.remove_class("visible")
        self._repaint()

    def hide(self) -> None:
        self._matches = []
        self._selected = 0
        self.remove_class("visible")
        self._repaint()

    def move_selection(self, delta: int) -> None:
        if not self._matches:
            return
        n = len(self._matches)
        self._selected = (self._selected + delta) % n
        self._repaint()

    def selected_command(self) -> SlashCommand | None:
        if not self._matches:
            return None
        return self._matches[self._selected]

    # ── rendering ─────────────────────────────────────────────────────────────

    def _repaint(self) -> None:
        if not self._matches:
            self.update("")
            return

        # Width for the command-name column (left)
        max_name_len = max(len(c.name) for c in self._matches)
        name_col = max(max_name_len + 1, 12)

        body = Text()
        for i, cmd in enumerate(self._matches):
            is_sel = i == self._selected
            row = Text()
            # Selection caret
            row.append("▌ " if is_sel else "  ",
                       style=_CORAL if is_sel else "#1a1a1a")
            # Command name
            name = f"/{cmd.name}".ljust(name_col)
            row.append(name, style=f"bold {_CORAL}" if is_sel else "#dddddd")
            row.append("  ")
            # Summary
            row.append(
                cmd.summary,
                style="#bbbbbb" if is_sel else "dim #888888",
            )
            if i > 0:
                body.append("\n")
            body.append_text(row)
        self.update(body)
