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
        prefix = self._prefix()
        arrow = "▼" if self._expanded else "▶"
        # Header is a 1-line Label, so use the first message line as the
        # synopsis. Only append the "…" overflow indicator when that first
        # line itself is too long — for multi-line messages whose first
        # line already fits (e.g. usage strings with sub-commands below),
        # the ▶/▼ arrow alone signals "expand for more" instead of a
        # misleading mid-sentence truncation marker.
        first_line, sep, _rest = self._message.partition("\n")
        if len(first_line) > 72:
            msg = first_line[:71] + "…"
        else:
            msg = first_line
        if prefix:
            return f"✗ {prefix}: {msg}  {arrow}"
        return f"✗ {msg}  {arrow}"

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label(self._header_text(), classes="eb-header")

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
            yield Static(detail_text, classes="eb-details")
            if has_trace:
                yield Label(
                    "Ctrl+B → events for full trace", classes="eb-hint",
                )
        else:
            # No details supplied — fall back to the full (untruncated) message
            # so long errors are still readable when the box is expanded.
            yield Static(self._message, classes="eb-details")
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
