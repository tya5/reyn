"""ConversationView — scrollable conversation pane.

Design decision (recorded here per design doc request):
  - Uses RichLog (not VerticalScroll+Static per-message) for the default
    renderer. RichLog gives us: append-only semantics, auto-scroll on new
    content, Rich markup / Text renderables out of the box, and a clean
    .clear() method for Ctrl+L. It is fast enough for streaming because we
    call .write() per coalesced chunk (16 ms window, not per token).
  - Intervention and permission widgets are mounted as child widgets in a
    VerticalScroll overlay *below* the RichLog, then removed when answered.
    This keeps the log clean while keeping interventions inline.

kind → prefix / style mapping (from design doc):
  agent      → "Aria  "  bold coral
  status     → "⟳ "      dim italic coral
  error      → "✗ "      bold red
  intervention → (InterventionWidget mounted — not a RichLog line)
  trace      → "· "      dim
  skill_done → "✓ "      bold green
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.widget import Widget
from textual.widgets import RichLog

from reyn.chat.outbox import OutboxMessage
from .intervention import InterventionWidget
from .streaming_row import StreamingRow


def _meta_prefix(meta: dict) -> str:
    """Build [skill#abcd] prefix from meta, same logic as renderer.py."""
    skill = meta.get("skill_name")
    short = meta.get("run_id_short")
    if skill and short:
        return f"[{skill}#{short}] "
    if skill:
        return f"[{skill}] "
    if short:
        return f"[#{short}] "
    return ""


class ConversationView(Widget):
    """Wraps a RichLog plus space for inline InterventionWidgets.

    Streaming:
      - `begin_stream(msg_id, agent_name)` → mounts a StreamingRow.
      - `append_stream(msg_id, text)` → calls StreamingRow.append().
      - `end_stream(msg_id)` → seals the row (stops accumulation).
    Non-streaming messages go through `render_message()`.
    """

    DEFAULT_CSS = """
    ConversationView {
        height: 1fr;
        border: tall #2a2a2a;
        padding: 0 0;
    }
    ConversationView RichLog {
        background: transparent;
        height: 1fr;
        border: none;
        scrollbar-color: #C8553D;
        padding: 0 1;
    }
    """

    def __init__(self, *, scroll_end: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._scroll_end = scroll_end
        self._stream_rows: dict[str, StreamingRow] = {}  # msg_id → row
        # Track whether user has scrolled up (suppress auto-scroll while scrolled)
        self._user_scrolled = False

    def compose(self) -> ComposeResult:
        yield RichLog(highlight=False, markup=False, wrap=True, id="log")

    def _log(self) -> RichLog:
        return self.query_one("#log", RichLog)

    # ── non-streaming message rendering ────────────────────────────────────────

    def render_message(self, msg: OutboxMessage) -> None:
        """Append a non-streaming OutboxMessage to the log."""
        if msg.kind == "intervention":
            # Interventions are handled via mount_intervention, not here.
            # Fall back to a plain log line for display when no callback wired.
            self._write_log(_format_intervention_line(msg))
            return
        if msg.kind == "agent":
            self._render_agent_markdown(msg)
            return
        text = _format_message(msg)
        if text is not None:
            self._write_log(text)

    def _render_agent_markdown(self, msg: OutboxMessage) -> None:
        """Render agent message as Markdown into the RichLog timeline.

        Earlier wave-B impl mounted Textual `Markdown` widgets as siblings
        below the RichLog, which broke chronological flow — user messages
        sat in RichLog while agent messages stacked separately at the
        bottom. Writing `rich.markdown.Markdown` directly into the RichLog
        keeps every turn in one append-only timeline (the original design)
        while still rendering markdown features (headers / lists / code
        blocks with syntax highlight via Rich's built-in renderer).
        """
        log = self._log()
        meta_pfx = _meta_prefix(msg.meta)
        prefix_text = f"agent  {meta_pfx}" if meta_pfx else "agent  "
        prefix = Text(prefix_text, style="bold #C8553D")
        log.write(prefix)
        if msg.text:
            log.write(RichMarkdown(msg.text))

    def _write_log(self, text: Text) -> None:
        log = self._log()
        log.write(text)

    # ── streaming support ──────────────────────────────────────────────────────

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        """Start a streaming agent message row. Returns the row widget."""
        prefix = f"{agent_name:<6}  " if agent_name else "agent  "
        row = StreamingRow(prefix=prefix, id=f"stream_{msg_id[:8]}")
        self._stream_rows[msg_id] = row
        # Mount inside a small wrapper so it sits after the RichLog
        self._log().write(Text(""))  # placeholder blank line for spacing
        # We mount the streaming row as a sibling of the log
        self.mount(row)
        return row

    def append_stream(self, msg_id: str, text: str) -> None:
        """Append text to an in-progress streaming row."""
        row = self._stream_rows.get(msg_id)
        if row is not None:
            row.append(text)

    def end_stream(self, msg_id: str) -> str:
        """Seal and remove a streaming row; returns accumulated text."""
        row = self._stream_rows.pop(msg_id, None)
        if row is None:
            return ""
        row.seal()
        # After sealing, the row stays in the DOM for the user to read.
        # The final text is available via row.full_text().
        return row.full_text()

    # ── intervention mounting ─────────────────────────────────────────────────

    def mount_intervention(
        self,
        *,
        question: str,
        choices: list[tuple[str, str]] | None = None,
        answer_callback=None,
        iv_id: str = "",
    ) -> InterventionWidget:
        """Mount an InterventionWidget inline below current log content."""
        widget = InterventionWidget(
            question=question,
            choices=choices,
            answer_callback=answer_callback,
            iv_id=iv_id,
        )
        self.mount(widget)
        widget.scroll_visible()
        return widget

    def clear(self) -> None:
        """Ctrl+L: clear the log (does not affect engine state)."""
        self._log().clear()
        # Seal any open streaming rows without removing them (they become static)
        for row in self._stream_rows.values():
            row.seal()
        self._stream_rows.clear()


# ── formatting helpers ─────────────────────────────────────────────────────────

def _format_message(msg: OutboxMessage) -> Text | None:
    """Convert an OutboxMessage to a Rich Text renderable.

    Returns None for kinds handled elsewhere (intervention → widget).
    """
    meta_pfx = _meta_prefix(msg.meta)
    body = f"{meta_pfx}{msg.text}"

    if msg.kind == "agent":
        t = Text()
        t.append("agent  ", style="bold #C8553D")
        t.append(body)
        return t
    if msg.kind == "status":
        t = Text()
        t.append("⟳ ", style="dim italic #C8553D")
        t.append(body, style="dim italic")
        return t
    if msg.kind == "error":
        t = Text()
        t.append("✗ ", style="bold red")
        t.append(body, style="bold red")
        return t
    if msg.kind == "trace":
        t = Text()
        t.append("· ", style="dim")
        t.append(body, style="dim")
        return t
    if msg.kind == "skill_done":
        t = Text()
        t.append("✓ ", style="bold green")
        t.append(body, style="bold green")
        return t
    if msg.kind in {"__end__", "__attach_request__", "intervention"}:
        return None
    # Unknown kind — show raw
    t = Text()
    t.append(f"[{msg.kind}] ", style="dim")
    t.append(body)
    return t


def _format_intervention_line(msg: OutboxMessage) -> Text:
    """Fallback inline line for intervention when no widget callback set."""
    t = Text()
    t.append("  Aria asks  ", style="bold #C8553D")
    t.append(msg.text, style="#ffcc88")
    return t
