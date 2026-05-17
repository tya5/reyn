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
        msg = self._message[:72] + "…" if len(self._message) > 72 else self._message
        if prefix:
            return f"✗ {prefix}: {msg}  {arrow}"
        return f"✗ {msg}  {arrow}"

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label(self._header_text(), classes="eb-header")

        if self._details:
            lines = self._details.splitlines()
            visible = lines[:5]
            overflow = len(lines) - 5
            detail_text = "\n".join(visible)
            if overflow > 0:
                detail_text += f"\n… {overflow} more"
            yield Static(detail_text, classes="eb-details")
            yield Label("Ctrl+B → events for full trace", classes="eb-hint")
        else:
            # No details supplied — fall back to the full (untruncated) message
            # so long errors are still readable when the box is expanded.
            yield Static(self._message, classes="eb-details")
            yield Label("Ctrl+B → events for full trace", classes="eb-hint")

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
