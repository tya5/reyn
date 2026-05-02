"""ReynHeader — top-of-screen status bar.

Displays: Reyn · <agent_name> · <model> · <tokens today> · <cost today>
on the left/centre, and a live clock (YYYY-MM-DD HH:MM:SS) on the right.

Updated via `app.post_message(ReynHeader.StatusUpdate(...))` or by calling
`refresh_status()` directly from async code. The clock self-ticks once
per second from on_mount.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label


class ReynHeader(Widget):
    """Single-line status bar docked at the top of the screen."""

    DEFAULT_CSS = """
    ReynHeader {
        dock: top;
        height: 1;
        background: #1a1a1a;
        layout: horizontal;
    }
    ReynHeader #title {
        color: #C8553D;
        text-style: bold;
        padding: 0 1;
        width: auto;
    }
    ReynHeader #status {
        color: #aaaaaa;
        text-align: right;
        padding: 0 1;
        width: 1fr;
    }
    """

    @dataclass
    class StatusUpdate(Message):
        """Posted to update the status fields without locking."""
        agent_name: str = ""
        model: str = ""
        tokens_today: int = 0
        tokens_cap: int | None = None
        cost_usd: float = 0.0
        cost_cap: float | None = None

    def __init__(
        self,
        *,
        agent_name: str = "",
        model: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._agent_name = agent_name
        self._model = model
        self._tokens_today = 0
        self._tokens_cap: int | None = None
        self._cost_usd = 0.0
        self._cost_cap: float | None = None

    def compose(self) -> ComposeResult:
        yield Label("Reyn", id="title")
        yield Label(self._format_status(), id="status")

    def on_mount(self) -> None:
        # Re-render once per second so the embedded clock stays current.
        # 1 Hz is plenty — seconds are included so a frozen UI is
        # immediately visible (the clock is the canary).
        self.set_interval(1.0, self._tick_clock)

    @staticmethod
    def _now_text() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _tick_clock(self) -> None:
        try:
            self.query_one("#status", Label).update(self._format_status())
        except Exception:
            pass

    def _format_status(self) -> Text:
        """Build the right-side status as a Rich Text with dim │ separators."""
        parts: list[str] = []
        if self._agent_name:
            parts.append(self._agent_name)
        if self._model:
            parts.append(self._model)
        if self._tokens_today > 0 or self._cost_usd > 0.0:
            tok_str = f"{self._tokens_today:,}"
            if self._tokens_cap is not None:
                tok_str += f" / {self._tokens_cap:,}"
            tok_str += " tok"
            cost_str = f"${self._cost_usd:.2f}"
            if self._cost_cap is not None:
                cost_str += f" / ${self._cost_cap:.2f}"
            parts.append(tok_str)
            parts.append(cost_str)
        # Clock always present, last — the canary for "is the UI frozen?"
        parts.append(self._now_text())

        out = Text()
        for i, p in enumerate(parts):
            if i > 0:
                out.append("  │  ", style="dim #555555")
            out.append(p)
        return out

    def refresh_status(
        self,
        *,
        agent_name: str | None = None,
        model: str | None = None,
        tokens_today: int | None = None,
        tokens_cap: int | None = None,
        cost_usd: float | None = None,
        cost_cap: float | None = None,
    ) -> None:
        """Update status fields and re-render. Call from async context."""
        if agent_name is not None:
            self._agent_name = agent_name
        if model is not None:
            self._model = model
        if tokens_today is not None:
            self._tokens_today = tokens_today
        if tokens_cap is not None:
            self._tokens_cap = tokens_cap
        if cost_usd is not None:
            self._cost_usd = cost_usd
        if cost_cap is not None:
            self._cost_cap = cost_cap
        try:
            label = self.query_one("#status", Label)
            label.update(self._format_status())
        except Exception:
            pass

    def on_reyn_header_status_update(self, msg: StatusUpdate) -> None:
        """Handle StatusUpdate message."""
        self.refresh_status(
            agent_name=msg.agent_name or None,
            model=msg.model or None,
            tokens_today=msg.tokens_today or None,
            cost_usd=msg.cost_usd,
            cost_cap=msg.cost_cap,
            tokens_cap=msg.tokens_cap,
        )
