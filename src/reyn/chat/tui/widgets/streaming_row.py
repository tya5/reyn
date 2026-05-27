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

# Wave-11 B#1 — tiered stall escalation thresholds. The previous
# single-tier ``> 5.0s`` cue gave identical visual signal whether
# the stream was paused for 5 s or hung for 5 minutes; users with
# a stuck LLM had no escalating prompt and Ctrl+C was the only
# escape. Three tiers now:
#
#   t < 5s       — live cursor blink (= normal)
#   5s ≤ t < 30s — dim " …" (= ambient stall, no fuss)
#   30s ≤ t < 60s— amber " … (stalled <fmt>)" (= attention)
#   t ≥ 60s      — red  " … (no token in <fmt>, Ctrl+C to cancel)"
#
# Boundaries match the SkillActivityRow elapsed-color thresholds
# (= 30 s amber, 60 s red) so the cross-widget mental model is
# consistent: 30 s = "taking a while", 60 s = "probably blocked".
_STALL_TIER_1_S = 5.0
_STALL_TIER_2_S = 30.0
_STALL_TIER_3_S = 60.0


def _fmt_idle(idle_seconds: float) -> str:
    """Human-readable elapsed for the stall cue.

    ``45s`` / ``1m`` / ``2m`` — minutes lose the trailing seconds
    because at this scale the user wants order-of-magnitude, not
    precision. Negative values clamp to ``0s`` so a clock-skew
    artifact can't surface as ``-3s``.
    """
    secs = max(0.0, idle_seconds)
    if secs < 60.0:
        return f"{int(secs)}s"
    return f"{int(secs // 60)}m"

# Body content rendered inside ConversationView lives at a dynamic
# hanging indent gated by ``_show_timestamps`` (8 with ts, 2 without).
# The StreamingRow is mounted while a reply is in flight (= before
# end_stream commits the final markdown). Since the streaming row is
# spawned after a header is written (timestamps already shown or not),
# it must use the ts-on value — the header write and the streaming body
# are in the same state snapshot. Kept as a local constant to avoid an
# import cycle (conversation already imports this module); must stay in
# sync with conversation._BODY_INDENT_WITH_TS (= _BODY_INDENT_COLS alias).
_BODY_INDENT_COLS = 8
# Public alias for cross-module invariant tests that check the
# ts-on indent stays in sync with ``conversation.BODY_INDENT_COLS``.
BODY_INDENT_COLS = _BODY_INDENT_COLS


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

    # Wave-9 F-F1 + F-F6: Static (streaming) and Markdown (sealed swap)
    # both get a 7-cell left padding so the body sits at the same
    # hanging indent as ``_write_body`` / ``_indent_body``. Without
    # this, the streaming text rendered at col 0 and visibly jumped 7
    # cells to the right at seal time when ``end_stream`` committed
    # the final markdown through ``_write_agent_markdown_with_fold``.
    # The 4-value form is ``top right bottom left``.
    DEFAULT_CSS = f"""
    StreamingRow {{
        padding: 0 0;
        height: auto;
    }}
    StreamingRow Static {{
        padding: 0 0 0 {_BODY_INDENT_COLS};
        height: auto;
    }}
    StreamingRow Markdown {{
        padding: 0 0 0 {_BODY_INDENT_COLS};
        height: auto;
        background: transparent;
    }}
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
        # Wave-9 F-F11: set when ``append()`` adds tokens (= the row's
        # rendered height may grow). Consumed by ``_flush_render`` to
        # gate the ``scroll_visible`` call so we only scroll on actual
        # content growth, not on cursor-blink ticks. Without the gate
        # the row would scroll-into-view 60 times per second during
        # idle blink — wasted work and a potential fight with the
        # user's intentional scroll-up.
        self._height_dirty: bool = False

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
            # Wave-9 F-F11: keep the growing row visible. ``StreamingRow``
            # is mounted as a sibling of the RichLog (not as a log line),
            # and ``RichLog.auto_scroll`` doesn't track sibling height
            # changes. On a short conversation the row's bottom slips
            # below the viewport as tokens wrap onto new lines, forcing
            # the user to manually scroll to see new content arrive.
            # ``scroll_visible`` walks up to the nearest scrollable
            # ancestor and brings this row into view. Gated by
            # ``_height_dirty`` so cursor-blink ticks (= rendered output
            # changed but height did NOT) don't fire 60 redundant
            # scroll calls per second.
            if self._height_dirty:
                self._height_dirty = False
                try:
                    self.scroll_visible(animate=False)
                except Exception:
                    pass

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
            # needed. Tiered stall cue (= wave-11 B#1):
            #   - 60s+ : red "no token in <fmt>, Ctrl+C to cancel"
            #            (= probably hung, user attention required)
            #   - 30s+ : amber "stalled <fmt>" (= taking a while)
            #   -  5s+ : dim "…" (= ambient pause)
            #   - else : live cursor blink
            idle = monotonic() - self._last_chunk_at
            if idle >= _STALL_TIER_3_S:
                t.append(
                    f" … (no token in {_fmt_idle(idle)}, Ctrl+C to cancel)",
                    style="bold #ff6644",
                )
            elif idle >= _STALL_TIER_2_S:
                t.append(
                    f" … (stalled {_fmt_idle(idle)})",
                    style="bold #ffaa44",
                )
            elif idle > _STALL_TIER_1_S:
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

            # Wave-9 F-F12: scope the sealed widget IDs by the row's
            # own id so two StreamingRow instances sealed concurrently
            # (e.g. ``/attach`` mid-stream, or remote ``--connect``
            # delivering an out-of-order ``__stream_end__`` for an
            # earlier turn) do not produce duplicate Textual widget
            # IDs in the DOM. Pre-fix both rows mounted children with
            # the hardcoded ``"sealed_prefix"`` / ``"sealed_markdown"``
            # ids — Textual logs a warning about duplicates and
            # ``query_one`` lookups become ambiguous. Fallback ``"anon"``
            # is defensive; the production caller always supplies an
            # id via ``begin_stream``.
            row_id = self.id or "anon"
            prefix_widget = Static(id=f"{row_id}_prefix")
            md_widget = Markdown(full, id=f"{row_id}_markdown")
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
        # Wave-9 F-F11: mark the row's height as "may have grown" so the
        # next ``_flush_render`` triggers ``scroll_visible``. Set here
        # (= where new tokens actually arrive) rather than on every
        # cursor blink, so idle re-renders don't spam scroll calls.
        self._height_dirty = True

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

    # ── Public accessors (Tier C Path C cleanup) ─────────────────────────────

    @property
    def sealed(self) -> bool:
        """True once ``seal()`` has been called (stream complete latch)."""
        return self._sealed

    @property
    def accumulated(self) -> str:
        """The concatenated text accumulated so far via ``append()`` calls."""
        return self._accumulated

    @property
    def interval_handle(self):  # type: ignore[return]
        """The ``set_interval`` handle for the 16 ms render-coalescing timer.

        ``None`` before ``on_mount`` or after ``seal()`` stops the timer.
        Exposed so tests can verify liveness without reaching into private state.
        """
        return self._interval_handle

    @property
    def height_dirty(self) -> bool:
        """True when ``append()`` has grown content since the last flush.

        Set by ``append()``; consumed (reset to False) by ``_flush_render``
        when it calls ``scroll_visible``.
        """
        return self._height_dirty

    @height_dirty.setter
    def height_dirty(self, value: bool) -> None:
        """Allow tests to reset the flag to establish a clean baseline."""
        self._height_dirty = value

    def build_renderable(self) -> "Text":
        """Public alias for ``_build_renderable()``.

        Returns the Rich ``Text`` object that would be pushed to the
        inner Static on the next flush — prefix + accumulated content +
        cursor/stall cue. Exposed so tests can inspect the rendered
        output without calling the private method directly, per the
        project testing policy (feedback_test_public_surface_not_private_state).
        """
        return self._build_renderable()
