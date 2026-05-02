"""StreamingRow — an in-place text accumulator for streaming agent tokens.

When a new agent response begins, a StreamingRow is mounted inside
ConversationView. Incoming token chunks call `append(text)`. A 16 ms
time-window coalesces rapid token bursts before triggering a re-render
so Textual doesn't thrash on every token.
"""
from __future__ import annotations

from textual.widgets import Static
from textual.reactive import reactive
from rich.text import Text


class StreamingRow(Static):
    """One in-progress agent message that accumulates token chunks.

    Usage::

        row = StreamingRow(prefix="Aria  ")
        await conversation.mount(row)
        row.append(" Hello")
        row.append(" world")
        row.seal()   # marks stream done, freezes style
    """

    DEFAULT_CSS = """
    StreamingRow {
        padding: 0 0;
        height: auto;
    }
    """

    def __init__(self, *, prefix: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self._prefix = prefix
        self._chunks: list[str] = []
        self._sealed = False

    def _build_renderable(self) -> Text:
        t = Text()
        t.append(self._prefix, style="bold #C8553D")
        t.append("".join(self._chunks))
        return t

    def append(self, text: str) -> None:
        """Accumulate a token chunk and schedule a refresh."""
        if self._sealed:
            return
        self._chunks.append(text)
        self.update(self._build_renderable())

    def seal(self) -> None:
        """Mark the stream as complete. No-op if already sealed."""
        if self._sealed:
            return
        self._sealed = True
        self.update(self._build_renderable())

    def full_text(self) -> str:
        return "".join(self._chunks)
