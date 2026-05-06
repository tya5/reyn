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

seal() behaviour:
  - Streaming text (raw Rich text) is displayed during streaming.
  - On seal(), the raw-text Static is hidden and a Textual Markdown widget
    is mounted so the final response renders with proper Markdown styling
    (headers, code blocks with syntax highlighting, lists, emphasis).
  - Partial Markdown is NOT attempted during streaming to avoid visual
    artifacts (unclosed code fences, broken lists, half-bold spans).
  - seal() may be called before the widget has finished composing (e.g.
    the engine seals a very short stream before Textual's first layout
    pass). In that case the Markdown swap is deferred to on_mount().
"""
from __future__ import annotations

_CORAL = "#C8553D"  # primary theme colour — matches Theme(primary=...)

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

_RENDER_INTERVAL_MS = 16  # ~60 fps max
_RENDER_INTERVAL_S = _RENDER_INTERVAL_MS / 1000


class StreamingRow(Widget):
    """One in-progress agent message that accumulates token chunks.

    During streaming, content is displayed as raw Rich text via an inner
    Static widget. On seal(), the Static is hidden and a Textual Markdown
    widget is mounted for proper Markdown rendering of the final response.

    Usage::

        row = StreamingRow(prefix="Aria  ")
        await conversation.mount(row)
        row.append(" Hello")
        row.append(" world")
        row.seal()   # swaps raw text for Markdown widget
    """

    DEFAULT_CSS = """
    StreamingRow {
        padding: 0 0;
        height: auto;
    }
    StreamingRow Static {
        padding: 0 0;
        height: auto;
    }
    StreamingRow Markdown {
        padding: 0 0;
        height: auto;
        background: transparent;
    }
    """

    def __init__(self, *, prefix: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self._prefix = prefix
        self._chunks: list[str] = []
        self._sealed = False
        self._dirty = False
        self._static: Static | None = None
        # True once the widget is in the DOM (compose + on_mount have run)
        self._mounted = False

    def compose(self) -> ComposeResult:
        self._static = Static(id="streaming_text")
        yield self._static

    def on_mount(self) -> None:
        """Start the 16 ms coalescing timer and do an initial render.

        If seal() was already called before we mounted (very fast stream),
        complete the Markdown swap now that the DOM is ready.
        """
        self._mounted = True
        self.set_interval(_RENDER_INTERVAL_S, self._flush_render)
        if self._sealed:
            # seal() ran before we were mounted — apply the Markdown swap now.
            self._apply_markdown_swap()
        else:
            self._flush_render()

    def _flush_render(self) -> None:
        """Render pending chunks if dirty. Called every 16 ms by interval."""
        if self._dirty and self._static is not None:
            self._dirty = False
            self._static.update(self._build_renderable())

    def _build_renderable(self) -> Text:
        t = Text()
        t.append(self._prefix, style="bold " + _CORAL)
        t.append("".join(self._chunks))
        return t

    def _apply_markdown_swap(self) -> None:
        """Hide the streaming Static and mount a Markdown widget in its place.

        Must only be called after the widget is mounted (self._mounted=True).
        Falls back to frozen raw text if the Markdown import fails.
        """
        full = "".join(self._chunks)
        try:
            from textual.widgets import Markdown  # type: ignore[import]

            if self._static is not None:
                self._static.display = False

            prefix_widget = Static(id="sealed_prefix")
            md_widget = Markdown(full, id="sealed_markdown")
            self.mount(prefix_widget, md_widget)
            prefix_widget.update(Text(self._prefix, style="bold " + _CORAL))
        except Exception:
            # Graceful fallback: freeze raw Rich text in the existing Static.
            if self._static is not None:
                self._static.display = True
                self._static.update(self._build_renderable())

    def append(self, text: str) -> None:
        """Accumulate a token chunk. Actual re-render happens on next flush."""
        if self._sealed:
            return
        self._chunks.append(text)
        self._dirty = True

    def seal(self) -> None:
        """Mark the stream as complete and swap to a Markdown widget.

        If the widget is already mounted, the swap happens immediately.
        If called before mount (very fast stream), the swap is deferred to
        on_mount() which runs as soon as Textual composes the widget.
        """
        if self._sealed:
            return
        self._sealed = True
        self._dirty = False

        if self._mounted:
            self._apply_markdown_swap()
        # else: on_mount() will call _apply_markdown_swap() when ready.

    def full_text(self) -> str:
        return "".join(self._chunks)
