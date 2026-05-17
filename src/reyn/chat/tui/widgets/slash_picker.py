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
from textual.message import Message
from textual.widgets import Static

from reyn.chat.slash import SlashCommand
from reyn.chat.tui._palette import _CORAL

_MAX_VISIBLE = 8


class SlashPicker(Static):
    """Inline list of slash-command matches shown above the TextArea."""

    class Clicked(Message):
        """Posted when the user clicks one of the picker rows.

        ``InputBar`` listens for this and calls ``_confirm_picker`` to
        insert the highlighted command — matching the keyboard Tab path.
        The picker stays ``can_focus = False`` so the click does not
        steal focus from the TextArea (the user keeps typing args).
        """

    DEFAULT_CSS = """
    SlashPicker {
        height: auto;
        /* 8 command rows + 1 "+N more" footer + 2 border rows = 11. */
        max-height: 11;
        background: #1a1a1a;
        border: solid $primary;
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
        # Total matches before truncation to _MAX_VISIBLE. Surfaced as a
        # "+N more — keep typing to filter" footer when the picker can't
        # show every match.
        self._total_matches: int = 0
        # Hint-only mode: when the user has typed "/<known-cmd> " (space
        # consumed, picker selection no longer relevant), show a single
        # informational row with the matched command's summary. ``_matches``
        # stays empty so Enter / Tab / arrows fall through to the normal
        # text-submit path and the user's typed args are preserved.
        self._hint_cmd: SlashCommand | None = None

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def visible_(self) -> bool:
        return self.has_class("visible")

    @property
    def has_matches(self) -> bool:
        return bool(self._matches)

    def set_matches(self, matches: list[SlashCommand]) -> None:
        """Replace the match list. Resets selection to row 0."""
        self._total_matches = len(matches)
        self._matches = list(matches)[:_MAX_VISIBLE]
        self._selected = 0
        self._hint_cmd = None
        if self._matches:
            self.add_class("visible")
        else:
            self.remove_class("visible")
        self._repaint()

    def set_hint(self, cmd: SlashCommand) -> None:
        """Show a single informational row for ``cmd`` (no selection caret).

        Used after the user types ``/<cmd> `` to remind them what the
        command does. Leaves ``_matches`` empty so the keyboard / click
        paths gated on ``has_matches`` skip; the user keeps typing args
        and Enter submits the typed text instead of replacing it with
        ``/cmdname``.
        """
        self._matches = []
        self._total_matches = 0
        self._selected = 0
        self._hint_cmd = cmd
        self.add_class("visible")
        self._repaint()

    def hide(self) -> None:
        self._matches = []
        self._selected = 0
        self._hint_cmd = None
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

    def select_at_y(self, content_y: int) -> bool:
        """Move the selection to the row at ``content_y`` (content-coord).

        ``content_y`` is the y-offset within the widget's content region
        (excluding border / padding). Returns True when the offset hit
        a valid match row; False when it landed on the "+N more" footer,
        a blank line, or beyond the visible matches.
        """
        if 0 <= content_y < len(self._matches):
            self._selected = content_y
            self._repaint()
            return True
        return False

    # ── mouse ────────────────────────────────────────────────────────────────

    def on_click(self, event) -> None:
        """Click on a row — confirm it.

        Slack/Discord muscle memory: clicking a suggestion inserts it.
        We compute the row from the content-relative offset (so the
        border + padding don't shift the math), update the selection,
        and post a ``Clicked`` message for InputBar to act on. The
        picker stays ``can_focus = False`` so focus does not move off
        the TextArea — the user can immediately type args.
        """
        offset = event.get_content_offset(self)
        if offset is None:
            return
        if self.select_at_y(offset.y):
            self.post_message(self.Clicked())

    # ── rendering ─────────────────────────────────────────────────────────────

    def _repaint(self) -> None:
        if not self._matches:
            if self._hint_cmd is not None:
                self._repaint_hint()
            else:
                self.update("")
            return

        # Width for the command-name column (left)
        max_name_len = max(len(c.name) for c in self._matches)
        name_col = max(max_name_len + 1, 12)

        # Truncate summary so each row stays on one visual line. Row layout
        # inside the picker box: caret(2) + name(name_col) + sep(2) + summary.
        # self.content_size lags during resize, so derive width from app.size
        # (live terminal width) minus the picker's overhead: border(2) +
        # padding(2) + an extra 2-cell margin (Textual sometimes wraps at the
        # boundary even when widths nominally match).
        try:
            term_w = self.app.size.width
        except Exception:
            term_w = 60
        content_w = max(20, term_w - 6)
        summary_budget = max(10, content_w - (2 + name_col + 2))

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
            # Summary (truncated to fit the row)
            summary = cmd.summary
            if len(summary) > summary_budget:
                summary = summary[: max(1, summary_budget - 1)] + "…"
            row.append(
                summary,
                style="#bbbbbb" if is_sel else "dim #888888",
            )
            if i > 0:
                body.append("\n")
            body.append_text(row)
        # When the typed prefix matches more commands than the picker
        # can display, surface the overflow so the user knows there's
        # more to discover by narrowing the prefix.
        hidden = self._total_matches - len(self._matches)
        if hidden > 0:
            body.append("\n")
            body.append(
                f"  +{hidden} more — keep typing to filter",
                style="dim #888888",
            )
        self.update(body)

    def _repaint_hint(self) -> None:
        """Render a single dim hint row showing the matched command's summary.

        No selection caret — the user has already chosen the command and is
        typing args. The picker stays out of the keyboard / click paths
        (``_matches`` is empty) so Enter / Tab / arrows fall through to the
        normal text-submit and history-recall behaviour.
        """
        cmd = self._hint_cmd
        if cmd is None:
            self.update("")
            return
        try:
            term_w = self.app.size.width
        except Exception:
            term_w = 60
        content_w = max(20, term_w - 6)
        name = f"/{cmd.name}"
        prefix_cells = 2 + len(name) + 2  # leading "  " + name + "  "
        summary_budget = max(10, content_w - prefix_cells)
        summary = cmd.summary
        if len(summary) > summary_budget:
            summary = summary[: max(1, summary_budget - 1)] + "…"
        t = Text()
        t.append("  ", style="#1a1a1a")
        t.append(name, style="dim #888888")
        t.append("  ")
        t.append(summary, style="dim #888888")
        self.update(t)
