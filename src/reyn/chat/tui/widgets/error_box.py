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
    /* Inline recovery hint extracted from the message — always visible
       (= no display:none) so users can read "• retry or check provider
       status" without expanding. Distinct color from `.eb-hint` so it
       reads as actionable, not just metadata. */
    ErrorBox Label.eb-inline-hint {
        color: #8a7a4a;
        height: 1;
        width: 1fr;
        padding: 0 2;
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
    ) -> None:
        super().__init__(id=id)
        self._message = message
        self._details = details
        self._run_id_short = run_id_short
        self._skill_name = skill_name
        self._expanded = False
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
        if len(first_line) > 72:
            msg = first_line[:71] + "…"
        else:
            msg = first_line
        msg = _markup_escape(msg)
        if prefix:
            return f"✗ {prefix}: {msg}  {arrow}"
        return f"✗ {msg}  {arrow}"

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label(self._header_text(), classes="eb-header")
        if self._inline_hint:
            yield Label(
                f"• {self._inline_hint}", classes="eb-inline-hint",
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
        """Toggle expanded/collapsed state."""
        self._expanded = not self._expanded
        self.toggle_class("-expanded")
        # Update the header arrow indicator
        try:
            header = self.query_one(".eb-header", Label)
            header.update(self._header_text())
        except Exception:
            pass
