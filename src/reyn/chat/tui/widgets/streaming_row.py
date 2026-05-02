"""StreamingRow — an in-place text accumulator for streaming agent tokens.

When a new agent response begins, a StreamingRow is mounted inside
ConversationView. Incoming token chunks call `append(text)`. A 16 ms
time-window coalesces rapid token bursts before triggering a re-render
so Textual doesn't thrash on every token.

Coalescing strategy:
  - `append()` stores the chunk and sets a _dirty flag.
  - A lightweight set_interval (16 ms) drives `_flush_render()` which
    re-renders only when dirty. This decouples engine chunk rate from
    Textual's render rate.
"""
from __future__ import annotations

import time

from textual.widgets import Static
from rich.text import Text

_RENDER_INTERVAL_MS = 16  # ~60 fps max
_RENDER_INTERVAL_S = _RENDER_INTERVAL_MS / 1000


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
        self._dirty = False
        self._last_render = 0.0

    def on_mount(self) -> None:
        """Start the 16 ms coalescing timer."""
        self.set_interval(_RENDER_INTERVAL_S, self._flush_render)

    def _flush_render(self) -> None:
        """Render pending chunks if dirty. Called every 16 ms by interval."""
        if self._dirty:
            self._dirty = False
            self.update(self._build_renderable())

    def _build_renderable(self) -> Text:
        t = Text()
        t.append(self._prefix, style="bold #C8553D")
        t.append("".join(self._chunks))
        return t

    def append(self, text: str) -> None:
        """Accumulate a token chunk. Actual re-render happens on next flush."""
        if self._sealed:
            return
        self._chunks.append(text)
        self._dirty = True

    def seal(self) -> None:
        """Mark the stream as complete and do a final render."""
        if self._sealed:
            return
        self._sealed = True
        self._dirty = False
        self.update(self._build_renderable())

    def full_text(self) -> str:
        return "".join(self._chunks)
