"""ReynHeader — top-of-screen status bar.

Displays: Reyn · <agent_name> · <model> · <tokens today> · <cost today>
on the left/centre, and a live clock (YYYY-MM-DD HH:MM:SS) on the right.

Updated via `app.post_message(ReynHeader.StatusUpdate(...))` or by calling
`refresh_status()` directly from async code. The clock self-ticks once
per second from on_mount.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label

# Trailing date suffix on a model id: ``-YYYYMMDD`` (8 digits) or the
# ``-YYYY-MM-DD`` form. Both rotate per release and add 9-11 cells to
# the header without changing within a session. Stripping them recovers
# ~25 cells of header width on a narrow terminal — the cost / token
# counters and the clock canary fit again without truncation.
_MODEL_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{1,2}-\d{1,2})$")
_MODEL_LATEST_SUFFIX = re.compile(r"-latest$")


def _shorten_model_id(model: str) -> str:
    """Return ``model`` with trailing date / ``-latest`` suffix stripped.

    Conservative: keeps the provider prefix (``claude-``, ``gpt-``, …) and
    everything up to the version segment so the user can still tell what
    family is active. Only strips the universally redundant tail.

    Examples::

        claude-opus-4-5-20251101    → claude-opus-4-5
        claude-3-7-sonnet-20250219  → claude-3-7-sonnet
        gpt-4o-2024-08-06           → gpt-4o
        gemini-1.5-flash-latest     → gemini-1.5-flash
        claude-sonnet-4-6           → claude-sonnet-4-6 (untouched)
    """
    if not model:
        return model
    stripped = _MODEL_DATE_SUFFIX.sub("", model)
    stripped = _MODEL_LATEST_SUFFIX.sub("", stripped)
    return stripped or model  # paranoia: never return empty


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
        color: $primary;
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
        # Issue #277 — count of stalled / cross-channel pending ops
        # surfaced as ``[N pending]`` badge on the status line when > 0.
        # Refreshed by ``set_stalled_count`` (called from the App layer
        # on the ``_refresh_live`` tick or right after an explicit
        # mutation). Omitted from the status entirely when 0 so the
        # cold-default UX is unchanged.
        self._stalled_count: int = 0

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
        # ``HH:MM:SS`` only (8 cells) — the date portion (10 cells +
        # separator = 11 saved) pushed the header past the 80-col
        # terminal boundary and clipped the clock half on cold-default
        # widths. The clock's primary role here is the "is the UI
        # frozen?" canary, which is the SECOND field — seconds are what
        # change visibly. The date is already surfaced once in the conv
        # pane via ``_date_separator`` at session start, so dropping it
        # from the per-second header avoids the redundancy without
        # losing day-level context.
        return time.strftime("%H:%M:%S")

    def _tick_clock(self) -> None:
        try:
            self.query_one("#status", Label).update(self._format_status())
        except Exception:
            pass

    def _format_status(self) -> Text:
        """Build the right-side status as a Rich Text with dim │ separators.

        Field layout (left → right):
          agent_name │ model │ tokens │ cost │ clock

        ``model`` is rendered DIM and date-suffix-stripped (e.g.
        ``claude-opus-4-5-20251101`` → ``claude-opus-4-5``). It rarely
        changes within a session, so de-emphasising it keeps the user's
        eye on the per-turn metrics that DO change — tokens, cost, and
        the clock canary at the right edge.
        """
        # (text, style) tuples — style=None falls back to the widget's
        # default text color (#aaaaaa, see DEFAULT_CSS above).
        parts: list[tuple[str, str | None]] = []
        if self._agent_name:
            parts.append((self._agent_name, None))
        if self._model:
            parts.append((_shorten_model_id(self._model), "dim #888888"))
        tok_str = f"{self._tokens_today:,}"
        if self._tokens_cap is not None:
            tok_str += f" / {self._tokens_cap:,}"
        tok_str += " tok"
        # Use 4 decimals so the cheap-model spend stays visible. With 2dp
        # `gemini-flash-lite` rounds to `$0.00` even after dozens of calls;
        # users see the token counter tick up but think the cost is free.
        # The cap (when set) is at a larger scale, so 2dp there is fine.
        cost_str = f"${self._cost_usd:.4f}"
        if self._cost_cap is not None:
            cost_str += f" / ${self._cost_cap:.2f}"
        parts.append((tok_str, None))
        parts.append((cost_str, None))
        # Issue #277 — pending-ops badge. Inserted before the clock so
        # the canary stays in its expected (= rightmost) position.
        # Amber-style colour to signal "user attention soft-required".
        # Omitted entirely when count is 0 — cold-default layout
        # unchanged.
        if self._stalled_count > 0:
            parts.append(
                (f"[{self._stalled_count} pending]", "#ffaa44"),
            )
        # Clock always present, last — the canary for "is the UI frozen?"
        parts.append((self._now_text(), None))

        out = Text()
        for i, (text, style) in enumerate(parts):
            if i > 0:
                out.append("  │  ", style="dim #555555")
            if style is None:
                out.append(text)
            else:
                out.append(text, style=style)
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
        stalled_count: int | None = None,
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
        if stalled_count is not None:
            self._stalled_count = max(0, int(stalled_count))
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
