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
  - A blinking block cursor (▍) is appended during streaming; it toggles
    every ~30 flush ticks (~480 ms). If no new chunk arrives for >5 s, the
    cursor is replaced with a dim ` …` stall indicator.

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

from time import monotonic

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import _AMBER

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
        # Running concatenation. Previously ``_chunks: list[str]`` was joined
        # via ``"".join(...)`` inside every render flush — that's O(N) per
        # flush and ``_flush_render`` fires at ~60 fps, so the total cost
        # grew O(N²) over an N-chunk stream (a 4000-token reply meant ~8 M
        # byte copies before seal). Appending to a single string + relying
        # on CPython's amortised O(1) single-owner string growth keeps the
        # cost linear.
        self._accumulated: str = ""
        self._sealed = False
        self._dirty = False
        self._static: Static | None = None
        # True once the widget is in the DOM (compose + on_mount have run)
        self._mounted = False
        # Cursor blink state
        self._cursor_visible: bool = True
        # Wave-9 F-F2: arm the idle timer from stream open (= row
        # construction). The sticky ``⟳ thinking…`` indicator hides at
        # ``__stream_start__`` — BEFORE the first token arrives — so a
        # cold-start LLM that takes 6-20s to deliver its first token
        # previously left the user with only a blinking cursor and no
        # "still waiting" cue (= the old ``_last_chunk_at = 0.0``
        # sentinel disabled the stall calculation until the first chunk
        # landed). With the timer armed from open, the stall ``  …``
        # indicator fires at the standard 5s threshold even when the
        # first chunk is still in flight.
        self._last_chunk_at: float = monotonic()
        self._cursor_tick_count: int = 0
        # Handle for the 16 ms render-coalescing interval, stored so
        # ``seal()`` can stop it. Without this the timer kept firing at
        # 60 Hz into a sealed (and possibly removed) widget for the rest
        # of the session — a 20-turn dogfood produced 20 × 60 = 1200 dead
        # callbacks/s on the event loop.
        self._interval_handle = None  # type: ignore[assignment]

    def compose(self) -> ComposeResult:
        self._static = Static(id="streaming_text")
        yield self._static

    def on_mount(self) -> None:
        """Start the 16 ms coalescing timer and do an initial render.

        If seal() was already called before we mounted (very fast stream),
        complete the Markdown swap now that the DOM is ready.
        """
        self._mounted = True
        self._interval_handle = self.set_interval(_RENDER_INTERVAL_S, self._flush_render)
        if self._sealed:
            # seal() ran before we were mounted — apply the Markdown swap now,
            # then stop the interval so it doesn't fire into a sealed widget.
            self._apply_markdown_swap()
            self._stop_interval()
        else:
            self._flush_render()

    def _flush_render(self) -> None:
        """Render pending chunks if dirty. Called every 16 ms by interval."""
        if not self._sealed:
            self._cursor_tick_count += 1
            if self._cursor_tick_count % 30 == 0:
                self._cursor_visible = not self._cursor_visible
                self._dirty = True
        if self._dirty and self._static is not None:
            self._dirty = False
            self._static.update(self._build_renderable())

    def _stop_interval(self) -> None:
        """Cancel the 16 ms render-coalescing interval, if running.

        Idempotent: safe to call on a row that was never mounted or whose
        timer was already stopped. Called from ``seal()`` and from the
        deferred-seal branch of ``on_mount``.
        """
        handle = self._interval_handle
        if handle is None:
            return
        try:
            handle.stop()
        except Exception:
            pass
        self._interval_handle = None

    def _build_renderable(self) -> Text:
        t = Text()
        t.append(self._prefix, style="bold " + _AMBER)
        t.append(self._accumulated)
        if not self._sealed:
            # ``_last_chunk_at`` is armed at construction (F-F2) so this
            # calculation is always meaningful — no sentinel-zero guard
            # needed. Idle past 5s shows the stall cue regardless of
            # whether any token has arrived yet.
            idle = monotonic() - self._last_chunk_at
            if idle > 5.0:
                t.append(" …", style="dim")
            else:
                if self._cursor_visible:
                    t.append("▍", style="bold " + _AMBER)
                else:
                    t.append(" ")
        return t

    def _apply_markdown_swap(self) -> None:
        """Hide the streaming Static and mount a Markdown widget in its place.

        Must only be called after the widget is mounted (self._mounted=True).
        Falls back to frozen raw text if the Markdown import fails.
        """
        full = self._accumulated
        try:
            from textual.widgets import Markdown  # type: ignore[import]

            if self._static is not None:
                self._static.display = False

            prefix_widget = Static(id="sealed_prefix")
            md_widget = Markdown(full, id="sealed_markdown")
            self.mount(prefix_widget, md_widget)
            prefix_widget.update(Text(self._prefix, style="bold " + _AMBER))
        except Exception:
            # Graceful fallback: freeze raw Rich text in the existing Static.
            if self._static is not None:
                self._static.display = True
                self._static.update(self._build_renderable())

    def append(self, text: str) -> None:
        """Accumulate a token chunk. Actual re-render happens on next flush."""
        if self._sealed:
            return
        self._last_chunk_at = monotonic()
        # CPython amortises ``s += x`` to O(1) when ``s`` is single-owner.
        # The flush path now reads this directly instead of joining a list
        # of chunks on every render tick.
        self._accumulated += text
        self._dirty = True

    def seal(self) -> None:
        """Mark the stream as complete and swap to a Markdown widget.

        If the widget is already mounted, the swap happens immediately and
        the 16 ms render interval is cancelled — without that cancel the
        timer kept firing at 60 Hz into a sealed (and possibly removed)
        widget for the rest of the session.
        If called before mount (very fast stream), both the swap and the
        cancel are deferred to on_mount().
        """
        if self._sealed:
            return
        self._sealed = True
        self._dirty = False

        if self._mounted:
            self._apply_markdown_swap()
            self._stop_interval()
        # else: on_mount() will call _apply_markdown_swap() + _stop_interval().

    def full_text(self) -> str:
        return self._accumulated
