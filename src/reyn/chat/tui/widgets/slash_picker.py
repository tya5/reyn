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
from reyn.chat.tui._palette import (
    _BG_HEADER,
    _CORAL,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MUTED,
    _TEXT_NEUTRAL,
    _TEXT_SELECTED,
)

from ._renderable_cache import RenderableCacheMixin

_MAX_VISIBLE = 8


class SlashPicker(RenderableCacheMixin, Static):
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
        background: #1a1a1a;  /* _BG_HEADER */
        border: solid $primary;
        padding: 0 1;
        display: none;
    }
    SlashPicker.visible, SlashPicker.hint-active {
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
        # Optional completion strings rendered below the hint row when the
        # command supplies a CompleterFn (e.g. /attach surfacing agent
        # names). Same informational-only contract as hint mode — no
        # selection caret, no keyboard intercept.
        self._completions: list[str] = []
        self._total_completions: int = 0
        # Unknown-command hint state. Set by ``set_unknown_hint`` when the
        # user types a slash token that doesn't prefix any known command
        # (= picker would otherwise silently hide, leaving the user with
        # no in-input feedback that they're typing nonsense). Renders a
        # single dim row reading ``unknown /<typed> — did you mean /<…>?``.
        self._unknown_token: str = ""
        self._unknown_suggestions: list[str] = []
        # ``RenderableCacheMixin`` provides ``_set_rendered_cache`` +
        # ``rendered_text``. Every ``self.update(...)`` call in
        # ``_repaint*`` pairs with ``self._set_rendered_cache(...)`` so
        # the cache stays in lockstep with the visible content.

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def selected_index(self) -> int:
        """Currently highlighted row index (0-based).

        Always 0 when no matches are loaded. Read-only; mutate via
        ``move_selection`` or ``select_at_y``.
        """
        return self._selected

    @property
    def visible_(self) -> bool:
        """True iff the picker is showing selectable matches.

        Hint mode (= ``/<cmd> <args>``) does NOT count as visible_ — it has
        no selection caret and the keyboard paths skip it via has_matches.
        Existing test contract pins "post-confirm picker is not visible_"
        so this predicate stays matches-driven.
        """
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
        self._completions = []
        self._total_completions = 0
        self._unknown_token = ""
        self._unknown_suggestions = []
        self.remove_class("hint-active")
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

        Display is gated by the separate ``hint-active`` CSS class — NOT
        ``visible`` — so the matches-driven ``visible_`` predicate stays
        false (existing test contract: "post-confirm picker is not
        visible_").
        """
        self._matches = []
        self._total_matches = 0
        self._selected = 0
        self._hint_cmd = cmd
        self._completions = []
        self._total_completions = 0
        self._unknown_token = ""
        self._unknown_suggestions = []
        self.remove_class("visible")
        self.add_class("hint-active")
        self._repaint()

    def set_completions(
        self, cmd: SlashCommand, completions: list[str],
    ) -> None:
        """Show ``cmd``'s hint plus a list of arg-name completions.

        Like ``set_hint`` but additionally renders the strings returned by
        ``cmd.completer(session)`` (already prefix-filtered upstream) as
        dim rows below the summary. Still informational only — no
        selection caret, no keyboard intercept; the user reads, types,
        and Enter submits the typed text.
        """
        self._matches = []
        self._total_matches = 0
        self._selected = 0
        self._hint_cmd = cmd
        self._total_completions = len(completions)
        self._completions = list(completions)[:_MAX_VISIBLE]
        self._unknown_token = ""
        self._unknown_suggestions = []
        self.remove_class("visible")
        self.add_class("hint-active")
        self._repaint()

    def set_unknown_hint(self, typed: str, suggestions: list[str]) -> None:
        """Show a single dim row reading ``unknown /<typed> — did you mean /<…>?``.

        Used when the user has typed a slash token (= ``/xxxx``) that
        doesn't prefix any registered command. Without this, the picker
        silently hides and the user gets no in-input feedback that the
        command is invalid — they only learn on submit, after the
        backend returns an "unknown command" error.

        ``typed`` is the typed token WITHOUT the leading ``/``;
        ``suggestions`` is the prefix-suggestion list (already capped
        upstream by ``suggest_for_unknown``). Same informational-only
        contract as hint mode: no caret, no keyboard intercept; the
        user keeps typing and Enter still submits whatever they have.
        """
        self._matches = []
        self._total_matches = 0
        self._selected = 0
        self._hint_cmd = None
        self._completions = []
        self._total_completions = 0
        self._unknown_token = typed
        self._unknown_suggestions = list(suggestions)
        self.remove_class("visible")
        # Re-use the same display gate as hint mode so the existing
        # ``visible_`` predicate (= matches-only) stays accurate and
        # downstream keyboard/click paths skip via ``has_matches``.
        self.add_class("hint-active")
        self._repaint()

    def hide(self) -> None:
        self._matches = []
        self._selected = 0
        self._hint_cmd = None
        self._completions = []
        self._total_completions = 0
        self._unknown_token = ""
        self._unknown_suggestions = []
        self.remove_class("visible")
        self.remove_class("hint-active")
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
            elif self._unknown_token:
                self._repaint_unknown_hint()
            else:
                self.update("")
                self._set_rendered_cache("")
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
                       style=_CORAL if is_sel else _BG_HEADER)
            # Command name
            name = f"/{cmd.name}".ljust(name_col)
            row.append(name, style=f"bold {_CORAL}" if is_sel else _TEXT_BRIGHT)
            row.append("  ")
            # Summary (truncated to fit the row)
            summary = cmd.summary
            if len(summary) > summary_budget:
                summary = summary[: max(1, summary_budget - 1)] + "…"
            row.append(
                summary,
                style=_TEXT_SELECTED if is_sel else "dim " + _TEXT_MUTED,
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
                style="dim " + _TEXT_MUTED,
            )
        self.update(body)
        self._set_rendered_cache(body)

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
            self._set_rendered_cache("")
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
        t.append("  ", style=_BG_HEADER)
        t.append(name, style="dim " + _TEXT_MUTED)
        t.append("  ")
        t.append(summary, style="dim " + _TEXT_MUTED)
        # Structured usage line — surfaced as a second hint row when
        # the command opts in via ``SlashCommand.usage``. Indented
        # under the summary with a ``↳`` connector so the eye groups
        # them as "one command, two informational rows". Commands
        # without usage stay 1-line (backward compatible).
        #
        # Wave-11 C#5: previously skipped when ``_completions`` was
        # non-empty — but the commands with both required args AND a
        # finite arg list (/attach, /memory view, /plan resume) were
        # exactly the ones that benefit most from showing usage.
        # The guard meant usage rarely rendered in practice. Now
        # always renders when ``cmd.usage`` is set; total row count
        # (= 1 summary + 1 usage + ≤ 8 completions + optional "+N
        # more") stays within the CSS ``max-height: 11`` budget.
        if cmd.usage:
            indent = " " * (2 + len(name) + 2)
            usage_budget = max(10, content_w - len(indent) - 9)  # 9 = "↳ usage: "
            usage_text = cmd.usage
            if len(usage_text) > usage_budget:
                usage_text = usage_text[: max(1, usage_budget - 1)] + "…"
            t.append("\n")
            t.append(indent + "↳ usage: ", style="dim " + _TEXT_NEUTRAL)
            t.append(usage_text, style="dim " + _TEXT_BODY)
        # Completion rows (if the command supplied a CompleterFn — e.g.
        # /attach surfacing agent names). Indented under the hint row,
        # one per line, dim, informational only.
        if self._completions:
            indent = " " * (2 + len(name) + 2)
            for c in self._completions:
                t.append("\n")
                t.append(indent + c, style="dim " + _TEXT_MUTED)
            hidden = self._total_completions - len(self._completions)
            if hidden > 0:
                t.append("\n")
                t.append(
                    indent + f"+{hidden} more — keep typing to filter",
                    style="dim " + _TEXT_DIM,
                )
        # Tab-recall discovery footer — only for /find, only when history
        # is non-empty. Surfaces the affordance ("Tab inserts a recent
        # query") that the module docstring documents but the picker hint
        # never previously mentioned. We import lazily to avoid a circular
        # dependency between the widget layer and the slash submodule.
        if cmd.name == "find":
            from reyn.chat.slash.find import find_history_has_entries
            if find_history_has_entries():
                indent = " " * (2 + len(name) + 2)
                t.append("\n")
                t.append(indent + "↳ Tab inserts a recent query", style="dim " + _TEXT_NEUTRAL)
        self.update(t)
        self._set_rendered_cache(t)

    def _repaint_unknown_hint(self) -> None:
        """Render the unknown-command in-input feedback line.

        Shape:
            unknown /xxxx — did you mean /find /save /help?

        The leading "unknown" is dim red so the user notices the
        feedback at a glance; the suggestion list is dim grey so it
        reads as helpful context, not as a separate error. Without
        ``suggest_for_unknown`` upstream we just say "did you mean
        /help?" because ``suggest_for_unknown`` always falls back
        to that.
        """
        typed = self._unknown_token
        suggestions = self._unknown_suggestions
        t = Text()
        t.append("  ", style=_BG_HEADER)
        t.append("unknown ", style="dim #d4756a")
        t.append(f"/{typed}", style="dim #d4756a")
        if suggestions:
            t.append("  — did you mean ", style="dim " + _TEXT_MUTED)
            for i, sug in enumerate(suggestions):
                if i > 0:
                    t.append(" ", style="dim " + _TEXT_MUTED)
                t.append(f"/{sug}", style="dim " + _TEXT_BODY)
            t.append("?", style="dim " + _TEXT_MUTED)
        self.update(t)
        self._set_rendered_cache(t)
