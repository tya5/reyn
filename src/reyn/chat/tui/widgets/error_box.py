"""ErrorLine — collapsible single-line error widget.

Replaces the old tall-bordered ErrorBox with a compact, click-to-expand line.

Default (collapsed):
    ✗ [skill#run_id]: error message  ▶

After click (expanded):
    ✗ [skill#run_id]: error message  ▼
      detail line 1
      detail line 2
      … N more  Ctrl+B → events

Press Esc to dismiss (handled by ConversationView / app.py via the
`_error_boxes` list — same API as before).
"""
from __future__ import annotations

from rich.cells import cell_len as _cell_len
from rich.markup import escape as _markup_escape
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label, Static


class ErrorBox(Widget):
    """Collapsible single-line error indicator.

    Collapsed by default; click anywhere to expand/collapse.

    Args:
        message:       The primary error message to display.
        details:       Optional multi-line detail text (e.g. traceback).
                       First 5 lines shown; remaining lines are summarised.
        run_id_short:  Short run ID suffix shown in the header prefix.
        skill_name:    Skill name shown in the header prefix.
        id:            Optional Textual widget ID.
    """

    DEFAULT_CSS = """
    ErrorBox {
        height: auto;
        margin: 0;
        padding: 0;
        /* Left bar — a non-color channel for "this is an error". Color alone
           (``#cc5555`` on a dark pane, ~3.5:1 contrast vs the surroundings)
           is right at the WCAG AA threshold for large text and below for
           small text. The vertical bar gives the eye a shape / position
           cue that survives quick scrolling and color-blind users. */
        border-left: solid #cc5555;
    }
    /* Header line — always visible */
    ErrorBox Label.eb-header {
        color: #cc5555;
        height: 1;
        width: 1fr;
        padding: 0 1;
    }
    ErrorBox:hover Label.eb-header {
        color: #ff7777;
    }
    /* Detail block — hidden until expanded */
    ErrorBox Static.eb-details {
        display: none;
        color: #777777;
        height: auto;
        width: 1fr;
        padding: 0 2;
    }
    ErrorBox Label.eb-hint {
        display: none;
        color: #555555;
        height: 1;
        width: 1fr;
        padding: 0 2;
    }
    /* Inline recovery hint extracted from the message. Default
       `display: none` (= matches `.eb-hint` symmetry, wave-10
       follow-up I-F12) so an accidentally-yielded empty hint
       doesn't grab a row of layout. The `.-has-content` modifier
       toggles it visible — applied at yield time so the visibility
       contract is "the Label was created WITH content, not just
       attached to the DOM". Distinct color from `.eb-hint` so it
       reads as actionable, not just metadata. */
    ErrorBox Label.eb-inline-hint {
        display: none;
        color: #8a7a4a;
        height: 1;
        width: 1fr;
        padding: 0 2;
    }
    ErrorBox Label.eb-inline-hint.-has-content {
        display: block;
    }
    /* Expanded state — reveal details */
    ErrorBox.-expanded Static.eb-details {
        display: block;
    }
    ErrorBox.-expanded Label.eb-hint {
        display: block;
    }
    """

    def __init__(
        self,
        *,
        message: str,
        details: str = "",
        run_id_short: str = "",
        skill_name: str = "",
        id: str | None = None,
        index: int = 0,
        total: int = 0,
    ) -> None:
        super().__init__(id=id)
        self._message = message
        self._details = details
        self._run_id_short = run_id_short
        self._skill_name = skill_name
        self._expanded = False
        # Wave-11 B#6 — per-box index badge for stacked errors. When
        # ``total > 1`` the header renders ``✗ [2/3] [skill#abcd]: …``
        # so a user landing on a focused box (= via F5/F6 jump) sees
        # which one of the stack they're reading. ``mount_error`` /
        # ``dismiss_last_error`` keep these values fresh via
        # ``set_index_total`` after every stack mutation. Defaults
        # (= 0/0) omit the badge so single-error rendering is
        # unchanged (cold-default + backward compat for any caller
        # that doesn't pass the new kwargs).
        self._index: int = index
        self._total: int = total
        # Extract trailing ``• <hint>`` from the first line so the header
        # can truncate the detail portion without silently dropping the
        # recovery hint — ``classify_router_error`` formats messages as
        # ``"router failed: [bucket] <long-detail> • <hint>"`` and the
        # previous 72-char header cap cut the ``• hint`` suffix off when
        # the provider repr was verbose. Rendering the hint on its own
        # always-visible label keeps the actionable signal in front of
        # the user without requiring an expand.
        first_line, _sep, _rest = message.partition("\n")
        if " • " in first_line:
            detail_part, _bullet, hint_part = first_line.partition(" • ")
            self._inline_hint = hint_part.strip()
            self._first_line_for_header = detail_part
        else:
            self._inline_hint = ""
            self._first_line_for_header = first_line

    # ── header text helpers ───────────────────────────────────────────────────

    def _prefix(self) -> str:
        if self._skill_name and self._run_id_short:
            return f"[{self._skill_name}#{self._run_id_short}]"
        if self._skill_name:
            return f"[{self._skill_name}]"
        if self._run_id_short:
            return f"[#{self._run_id_short}]"
        return ""

    def _header_text(self) -> str:
        """Build the header line for a textual ``Label`` (Rich-markup aware).

        The error message and prefix are escaped via ``rich.markup.escape``
        because they originate from arbitrary error text — e.g. the
        agent-name validator emits ``"must be 1-32 chars of [a-z0-9_-]
        starting with [a-z0-9]"``, and the character class brackets were
        being consumed as Rich markup tags. That left the rendered header
        as ``"must be 1-32 chars of  starting…"`` (charset silently
        missing) and similar truncation for any error mentioning a regex,
        a list literal, or anything else bracket-shaped.
        """
        prefix = _markup_escape(self._prefix())
        arrow = "▼" if self._expanded else "▶"
        # Header is a 1-line Label, so use the first message line as the
        # synopsis. Only append the "…" overflow indicator when that first
        # line itself is too long — for multi-line messages whose first
        # line already fits (e.g. usage strings with sub-commands below),
        # the ▶/▼ arrow alone signals "expand for more" instead of a
        # misleading mid-sentence truncation marker. The ``• <hint>``
        # tail (when present) lives on its own ``.eb-inline-hint`` label,
        # so don't include it in the header truncation budget.
        first_line = self._first_line_for_header
        # Wave-10 follow-up I-F4: cell-aware truncation. ``len()`` counts
        # code points, but the header is rendered in a 1-line ``Label``
        # whose visual budget is measured in terminal cells. CJK / emoji
        # consume 2 cells per character, so a 72-code-point CJK header
        # is ~144 cells — far past the typical conv-pane width — and
        # silently wraps to a second line, breaking the ``height: 1``
        # CSS contract. Match the sticky_status / events_tab idiom
        # (= ``rich.cells.cell_len`` for budget, walk char-by-char to
        # build the truncated body so each ``…`` reservation is exact).
        if _cell_len(first_line) > 72:
            out_chars: list[str] = []
            used = 0
            for ch in first_line:
                w = _cell_len(ch)
                if used + w > 71:  # reserve 1 cell for "…"
                    break
                out_chars.append(ch)
                used += w
            msg = "".join(out_chars) + "…"
        else:
            msg = first_line
        msg = _markup_escape(msg)
        # Wave-11 B#6 — render a ``[N/M]`` index badge between ✗ and
        # the prefix when this row is part of a stack (total > 1).
        # Single-error renders omit the badge entirely so the
        # cold-default layout is unchanged. Badge sits BEFORE the
        # skill prefix so the eye reads "this is error 2 of 3 in
        # the [code_review#abc1] skill". Defensive ``getattr`` keeps
        # the ``__new__``-bypass-init test path working without
        # forcing every test to seed the fields.
        idx = getattr(self, "_index", 0)
        tot = getattr(self, "_total", 0)
        index_badge = (
            f"[{idx}/{tot}] "
            if tot > 1 and idx > 0
            else ""
        )
        if prefix:
            return f"✗ {index_badge}{prefix}: {msg}  {arrow}"
        return f"✗ {index_badge}{msg}  {arrow}"

    def set_index_total(self, index: int, total: int) -> None:
        """Update the stack-position badge and re-render the header.

        Called by ``ConversationView`` after every mutation to the
        ``_error_boxes`` list (= new mount, dismiss, auto-eviction)
        so the badge stays accurate. Single-error states (= total
        ≤ 1) hide the badge automatically.

        Idempotent — repeated calls with the same values skip the
        DOM round-trip so churn on a per-render tick stays cheap.
        """
        if self._index == index and self._total == total:
            return
        self._index = index
        self._total = total
        try:
            self.query_one(".eb-header", Label).update(self._header_text())
        except Exception:
            pass

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label(self._header_text(), classes="eb-header")
        if self._inline_hint:
            # Wave-10 follow-up I-F12: tag the Label with
            # ``-has-content`` so the CSS visibility toggles to
            # ``display: block``. The ``if`` guard above is still the
            # primary gate (= we don't mount empty Labels at all);
            # the class is defense-in-depth so a future refactor that
            # drops the guard or yields the Label with empty content
            # doesn't grab a layout row for an empty hint.
            yield Label(
                f"• {self._inline_hint}",
                classes="eb-inline-hint -has-content",
            )

        # The trace hint only makes sense when this error came from a
        # skill / op run — slash-command usage errors (= no skill_name,
        # no run_id_short) don't emit events, so pointing the user at
        # ``Ctrl+B → events`` would send them to a tab that has no row
        # for the failure they just saw.
        has_trace = bool(self._skill_name or self._run_id_short)

        if self._details:
            lines = self._details.splitlines()
            visible = lines[:5]
            overflow = len(lines) - 5
            detail_text = "\n".join(visible)
            if overflow > 0:
                detail_text += f"\n… {overflow} more"
            yield Static(_markup_escape(detail_text), classes="eb-details")
            if has_trace:
                yield Label(
                    "Ctrl+B → events for full trace", classes="eb-hint",
                )
        else:
            # No details supplied — fall back to the full (untruncated) message
            # so long errors are still readable when the box is expanded.
            yield Static(_markup_escape(self._message), classes="eb-details")
            if has_trace:
                yield Label(
                    "Ctrl+B → events for full trace", classes="eb-hint",
                )

    # ── interaction ───────────────────────────────────────────────────────────

    def on_click(self) -> None:
        """Toggle expanded/collapsed state atomically.

        Wave-10 follow-up I-F9: previously the three operations
        (``_expanded`` flip, ``toggle_class("-expanded")``, header
        ``update``) were three independent statements. The header
        update was wrapped in a bare ``try / except Exception: pass``,
        but the class toggle was not — and the ``_expanded`` flag was
        flipped BEFORE either DOM operation. Net effect on partial
        failure: the widget's internal state said "expanded" but the
        DOM state (= the CSS class controlling the body's display
        AND the arrow glyph in the header) stayed unchanged. The
        next click would then re-flip ``_expanded`` while the DOM
        was still in the original state, doubling the drift.

        Rewritten as: capture old state → attempt both DOM mutations
        in a single try → on success, flip ``_expanded``; on failure,
        roll back any partial DOM mutation so the widget stays in a
        coherent state.
        """
        new_expanded = not self._expanded
        try:
            self.toggle_class("-expanded")
            old_expanded = self._expanded
            self._expanded = new_expanded
            try:
                header = self.query_one(".eb-header", Label)
                header.update(self._header_text())
            except Exception:
                # Header update failed AFTER toggle_class succeeded — roll
                # both back so the widget stays internally consistent.
                self._expanded = old_expanded
                try:
                    self.toggle_class("-expanded")
                except Exception:
                    # Best-effort rollback; widget mid-teardown is the
                    # only realistic failure here.
                    pass
        except Exception:
            # toggle_class itself failed — _expanded was not flipped,
            # so no state drift.
            pass
