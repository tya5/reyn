"""ErrorBox — tall red-bordered error display widget.

Replaces single-line '✗ error_text' log lines with a visually prominent
bordered box that includes the error message, optional details, and a hint
directing the user to the right panel's events tab.

Design: bright-red border, dim-coral hint text, display-only (no interaction).
"""
from __future__ import annotations

_CORAL = "#C8553D"  # used for hint text only, NOT border

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label, Static


class ErrorBox(Widget):
    """Inline error display box with red border.

    Args:
        message:       The primary error message to display.
        details:       Optional multi-line detail text (e.g. traceback).
                       First 3 lines are shown; remaining lines are summarised.
        run_id_short:  Short run ID suffix shown in the header prefix.
        skill_name:    Skill name shown in the header prefix.
        id:            Optional Textual widget ID.
    """

    DEFAULT_CSS = """
    ErrorBox {
        border: solid #cc4444;
        padding: 1 2;
        height: auto;
        margin: 1 0;
    }
    ErrorBox Label.eb-header {
        text-style: bold;
        color: #cc4444;
        height: auto;
        width: 1fr;
        padding-bottom: 1;
    }
    ErrorBox Label.eb-message {
        color: #ff8866;
        height: auto;
        width: 1fr;
        padding-bottom: 1;
    }
    ErrorBox Static.eb-details {
        color: #888888;
        height: auto;
        width: 1fr;
    }
    ErrorBox Label.eb-hint {
        color: #888888;
        height: auto;
        width: 1fr;
        text-align: right;
        padding-top: 1;
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

    def _build_header(self) -> str:
        """Build header label text with optional [skill#run_id] prefix."""
        prefix = ""
        if self._skill_name and self._run_id_short:
            prefix = f"[{self._skill_name}#{self._run_id_short}]  "
        elif self._skill_name:
            prefix = f"[{self._skill_name}]  "
        elif self._run_id_short:
            prefix = f"[#{self._run_id_short}]  "
        return f"{prefix}✗ ERROR"

    def compose(self) -> ComposeResult:
        yield Label(self._build_header(), classes="eb-header")
        yield Label(self._message, classes="eb-message")

        if self._details:
            lines = self._details.splitlines()
            visible = lines[:3]
            overflow = len(lines) - 3

            detail_text = "\n".join(visible)
            if overflow > 0:
                detail_text += f"\n… {overflow} more"

            yield Static(detail_text, classes="eb-details")
            yield Label("[B→events]", classes="eb-hint")
