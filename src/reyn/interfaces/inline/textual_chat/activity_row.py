"""``ActivityRow`` — the one-line "what is happening right now" region (#3693).

The RUNNING gutter inside the conversation is the right local expression of a
live turn, but it scrolls away: read back through a long reply and there is
nothing near the composer saying whether the turn is still alive. This region
sits directly above the sent queue and answers that, for the duration of the
turn only.

It is a PROJECTION of state, never a decoration. Everything it can say has to
be something the client actually observed:

- ``WORKING`` — a turn is running and nothing more specific is known. This is
  also the correct answer for a client that attached mid-turn: it knows
  ``turn_active`` and nothing else.
- ``RESPONDING`` — content deltas are arriving.
- ``TOOL <label>`` — a tool call is in flight and its label is known.

The elapsed time is shown ONLY when a real start instant was observed. A
client that joined mid-turn has no start, so it prints no clock rather than a
number derived from when it happened to connect — the same rule the tool-timer
already follows, and the reason this module does not fall back to "now" when
the start is missing.

Deliberately not focusable and deliberately one line: the queue below owns
selection and cancellation, and a second focusable region between the queue
and the composer would put a stop on the way back to typing.

The row used to open with a literal ``NOW`` label. Owner call (2026-08-07,
"now とか next という文字列がださいな"): dropped it, and the state word
itself now carries a travelling highlight ("shine", design "A" of three
text-mockup options put to the owner) while a turn runs — see
:data:`_SHINE_WIDTH`, :func:`_shine_ramp` and :meth:`ActivityRow.tick`. The
band was a two-character ``reverse`` when #3779 first shipped it; the operator
read that as a block blinking rather than a light moving, which is what a
two-valued band is — it has no edge to fall off. #3777 replaced it with a
cosine ramp between :data:`palette.SHINE_DIM` and :data:`palette.SHINE_PEAK`,
keeping the old attribute band as the no-colour fallback. One
timer drives both the shine and the elapsed clock (the clock is recomputed
fresh from the real clock on every tick regardless of rate, so sharing the
timer costs nothing in correctness) and is paused/resumed by
:meth:`ActivityRow.end`/:meth:`begin` rather than left running while hidden
— an animation ticking over an idle ssh session for a row nobody sees is
exactly the cost that split exists to avoid.

This row carries NO glyph of its own (#3777, owner call). It briefly had
one — a filled ``▶`` pairing with the queue's hollow ``▷`` — and the shape
pair survived the removal by moving: ``▶`` is now what a SELECTED queue row
shows, so "filled" still means "the one being acted on" and the row above no
longer has to say so twice. What this row keeps is the alignment: its text
starts in the column a queue row's LABEL starts in
(:data:`~reyn.interfaces.inline.textual_chat.sent_queue.ROW_TEXT_COLUMN`),
so the two regions read as one column of text with the queue's glyphs hanging
off its left edge.
"""
from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Callable

from rich.color import Color, blend_rgb
from textual.content import Content
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.timer import Timer

from reyn.interfaces.inline.textual_chat import palette
from reyn.interfaces.inline.textual_chat.sent_queue import ROW_TEXT_COLUMN

#: The cancel affordance shown while a turn runs. Plain ASCII: the key it names
#: is the app's own ``ctrl+c`` binding, and this row shares a narrow terminal
#: with the queue below it.
_CANCEL_HINT = "Ctrl+C cancel"

#: The key that returns to the newest output (#3712). Named here so the row and
#: the binding that implements it cannot drift: a printed key that does not
#: work is worse than none, and the conversation pane's own ``end``/``G`` only
#: fire while it holds focus — i.e. never from where the reader actually is.
LATEST_HINT = "Ctrl+End"

#: How wide the travelling highlight band is, in characters. Odd, so the band
#: has ONE centre cell for the cosine to peak on rather than two cells tied
#: for brightest — a two-cell peak is what makes a band read as a block that
#: blinks instead of a light that moves. Five rather than rich's twenty
#: (``PULSE_SIZE``): rich sizes its band against a full-width progress bar,
#: and ours travels through ``state`` alone, which is ten characters at
#: ``"RESPONDING"``. A band wider than half its own runway never resolves as
#: a band at all.
_SHINE_WIDTH = 5

#: The shine where the terminal reports no usable colour. The band degrades to
#: what #3779 shipped — a two-character ``reverse`` — rather than to nothing:
#: an SGR attribute survives every ansi theme, so the row still shows that a
#: turn is running where a colour would have shown nothing. This is the same
#: capability split rich makes in ``_get_pulse_segments`` (it drops to a flat
#: two-tone bar outside ``standard``/``eight_bit``/``truecolor``), reached
#: here from the same reasoning rather than by copying its branch: a gradient
#: needs colour, and where there is none the honest fallback is the emphasis
#: that does not.
_SHINE_FALLBACK_STYLE = "reverse"
_SHINE_FALLBACK_WIDTH = 2



@lru_cache(maxsize=1)
def _shine_ramp() -> "tuple[str, ...]":
    """One colour per cell of the band, brightest first.

    Index is the cell's DISTANCE from the band's centre, so ``[0]`` is the
    peak and the last entry is the faintest cell still painted; anything
    further out is left unstyled and keeps the terminal's own foreground.
    Storing it by distance rather than by absolute position is what lets the
    band slide without recomputing: the ramp is a property of the band, and
    only where its centre sits changes per frame.

    The shape is a raised cosine — ``0.5 + cos(pi * d / half) / 2`` — which is
    the same curve rich's ``_get_pulse_segments`` uses, reached by calling its
    ``blend_rgb`` rather than by copying it. rich's ``ProgressBar`` itself is
    no use to us: it is a component that draws its OWN bar, not one that
    lights up someone else's text, so the only reusable part was the blend,
    and that is already public API.

    Cached because the ramp is fixed for the life of the process and this runs
    at ``SHINE_FPS`` — recomputing five blends per frame, forever, to arrive
    at the same five strings is the kind of cost that never shows up in a
    profile and never stops being paid.
    """
    dim = Color.parse(palette.SHINE_DIM).get_truecolor()
    peak = Color.parse(palette.SHINE_PEAK).get_truecolor()
    half = _SHINE_WIDTH // 2
    ramp: "list[str]" = []
    for distance in range(half + 1):
        fade = 1.0 if half == 0 else 0.5 + math.cos(math.pi * distance / half) / 2.0
        blended = blend_rgb(dim, peak, cross_fade=fade)
        ramp.append(f"#{blended.red:02x}{blended.green:02x}{blended.blue:02x}")
    return tuple(ramp)


def activity_text(
    state: str,
    *,
    elapsed_s: "float | None" = None,
    width: int = 80,
    behind: "int | None" = None,
    shine_index: "int | None" = None,
    colour: bool = True,
) -> Content:
    """The rendered row for ``state``.

    ``elapsed_s`` is omitted entirely when ``None`` — an unknown duration
    prints no clock rather than a zero, because "00:00" reads as a fact and
    "no clock" reads as what it is.

    ``behind`` (#3712) is how many entries have landed since the reader left
    the newest output, or ``None`` while they are still on it. It takes the
    right-hand slot ahead of the cancel hint: someone reading back through an
    older reply cannot see the new output arriving, and that is the more
    urgent of the two. Both are dropped before the state itself.

    ``shine_index`` (owner-picked design "A", replacing the removed ``NOW``
    label): the frame number of a ``_SHINE_WIDTH``-character band travelling
    across the WHOLE rendered row — ``None`` paints no band (a static row,
    e.g. while no turn is showing). It is a frame counter, not a character
    offset: the band's centre is ``shine_index - _SHINE_WIDTH // 2``, so it
    starts off the left edge and runs off the right.

    #3779 confined the band to ``state``'s own span, so it could not wander
    onto the elapsed clock or the hint. #3777 lifted that: with the glyph gone
    and the row reading as ONE object rather than as three things sharing a
    line, the owner asked for the light to cross all of it. The old argument
    was not wrong — it was the right answer for the row it was written about.

    ``colour`` is whether the terminal can show one. ``True`` paints the band
    as a cosine ramp between :data:`palette.SHINE_DIM` and
    :data:`palette.SHINE_PEAK`; ``False`` degrades it to the two-character
    ``reverse`` #3779 shipped. The gradient is the point — a two-valued band
    has no edge to fall off, so it reads as a block blinking rather than a
    light travelling, which is what the operator reported.
    """
    # Spaces, not a glyph (#3777, owner call): the NOW row carries no mark of
    # its own, and its text starts in the column a queue row's LABEL starts in
    # — the two regions then read as one column of text with the queue's
    # glyphs hanging off its left edge, rather than as two lists that happen to
    # sit above each other. The width comes from the queue rather than being
    # written here twice; see ``sent_queue.ROW_TEXT_COLUMN``.
    prefix = " " * ROW_TEXT_COLUMN if state else ""
    body = f"{prefix}{state}"
    if elapsed_s is not None:
        body = f"{body} {int(elapsed_s) // 60:02d}:{int(elapsed_s) % 60:02d}"
    suffix = f"LIVE +{behind} · {LATEST_HINT} latest" if behind else _CANCEL_HINT
    content = _with_suffix(body, suffix, width)
    if shine_index is not None and state:
        # The whole row, not just the state word (#3777, owner call). #3779
        # confined the band to ``state`` so it could not wander onto the clock
        # or the hint; with the glyph gone and the row reading as one object,
        # the owner asked for the light to cross all of it. The clearance
        # argument that produced the old confinement is not wrong — it was an
        # answer to a row that was three separate things in a line.
        content = _apply_shine(content, 0, len(content.plain), shine_index, colour)
    return content


def _apply_shine(
    content: Content, base: int, span: int, frame: int, colour: bool
) -> Content:
    """The travelling band, painted over ``span`` characters starting at ``base``.

    ``base`` is where ``state`` begins in the rendered line; every offset below
    is relative to ``state`` and shifted by it, so the band never reaches back
    over the leading glyph, the space after it, the elapsed clock, or the
    right-hand hint. Those are different information and stay plain — the
    clearance #3777 required.

    ``frame`` is wrapped here rather than by the caller, because the cycle's
    length depends on ``span`` — i.e. on ``len(state)``, which changes every
    time the state does. A widget that owned the wrap would have to re-derive
    it on each ``specialise``, and a frame counter left over from the previous
    state would silently sit outside the new cycle and paint nothing while a
    turn was visibly running. Wrapping where the length is known makes any
    integer a valid frame.

    The band's centre is ``frame - half``, so the cycle starts with the centre
    OFF the left edge and ends with it past the right. That is what makes the
    light enter and leave, rather than materialise at the first character and
    vanish at the last — clamp the centre inside the span instead and the two
    end frames hold a stationary half-band, which reads as a stutter at both
    ends of every pass.
    """
    half = _SHINE_WIDTH // 2
    # ``span + 2 * half``, not ``span + _SHINE_WIDTH``: the centre must reach
    # from ``-half`` (leftmost cell just lit) to ``span - 1 + half`` (rightmost
    # cell still lit) and no further. One frame more and the band sits entirely
    # off the right edge — a single dark frame per pass, which at SHINE_FPS is
    # a blink in an animation whose whole job is to look continuous.
    frame %= max(1, span + 2 * half)
    if not colour:
        # Same two characters #3779 shipped, so the degraded form is a form
        # this row has already been seen in rather than a third design.
        start = base + max(0, min(frame - half, span - 1))
        return content.stylize(
            _SHINE_FALLBACK_STYLE, start, min(start + _SHINE_FALLBACK_WIDTH, base + span)
        )
    centre = frame - half
    for distance, style in enumerate(_shine_ramp()):
        # Both sides of the centre at this distance — and only once when
        # distance is 0, or the centre cell would be styled twice.
        for position in {centre - distance, centre + distance}:
            if 0 <= position < span:
                content = content.stylize(style, base + position, base + position + 1)
    return content


def _with_suffix(body: str, suffix: str, width: int) -> Content:
    """``body`` with ``suffix`` right-aligned, or ``body`` alone if it will not
    fit WHOLE.

    Never clipped. A cut ``Ctrl+C cancel`` ends as ``Ctrl+C``, which still
    reads as a complete instruction and is a different one — the row would be
    naming a key combination nobody chose. The same applies to a cut
    ``Ctrl+End latest``. Dropping it costs nothing: both bindings work whether
    or not they are printed.
    """
    pad = width - len(body) - len(suffix)
    text = f"{body}{' ' * pad}{suffix}" if pad >= 1 else body
    return Content(text)


class ActivityRow(Static):
    """The live-turn line. Hidden whenever no turn is running."""

    # No colour declaration, deliberately. The first version said
    # ``color: @quiet@``, and measured under ``ansi-dark`` that resolves to the
    # SAME value as ordinary text (``Color(0, 0, 0, ansi=-1)``, identical to
    # StatusLine's) — it receded by exactly nothing, which is #3523's defect in
    # a brand-new site. But ``dim`` is not the fix here either: this row exists
    # to say a turn is live while the operator is reading somewhere else, so it
    # is not a thing that should recede. Ordinary brightness is the honest
    # rendering, and stating no colour is how it is asked for.
    DEFAULT_CSS = palette.css("""
    ActivityRow {
        height: 1;
        padding: 0 1;
    }
    """)

    can_focus = False

    def __init__(self, *, clock: "Callable[[], float]" = time.monotonic, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._clock = clock
        self._state: "str | None" = None
        self._started_at: "float | None" = None
        self._behind: "int | None" = None
        self._shine_index = 0
        self._timer: "Timer | None" = None

    #: The shine's frame rate (owner-picked design "A"). Also what drives the
    #: elapsed clock now — ONE timer, not two: :meth:`tick` recomputes the
    #: elapsed seconds from the real clock on every call regardless of how
    #: often it runs, so a faster shared tick costs nothing in correctness,
    #: only ``_SHINE_WIDTH``-worth of extra string work per frame (one Static
    #: line, cheap) — and it means there is exactly one place that starts and
    #: stops the redraw, not two timers each needing their own shutdown.
    SHINE_FPS = 6

    def on_mount(self) -> None:
        self.display = False
        # Created PAUSED: the animation must not run — must not even be
        # firing no-op ticks — while no turn is showing. A timer that keeps
        # waking the event loop over an idle ssh session for nothing this
        # row will ever draw is exactly the cost :meth:`begin`/:meth:`end`
        # exist to bound. ``resume``/``pause`` there are this timer's ONLY
        # two callers.
        self._timer = self.set_interval(1 / self.SHINE_FPS, self.tick, pause=True)

    @property
    def state(self) -> "str | None":
        """The state currently shown, or ``None`` while the row is hidden."""
        return self._state

    def begin(self, state: str = "WORKING", *, started: bool = True) -> None:
        """Show the row for a running turn.

        ``started=False`` marks a turn this client did not see begin (a
        mid-turn attach): the row appears, and no elapsed time is ever shown
        for it, because there is no instant to measure from.
        """
        if self._state is None:
            self._started_at = self._clock() if started else None
            self._shine_index = 0
            if self._timer is not None:
                self._timer.resume()
        self._state = state
        self.display = True
        self._render_row()

    def specialise(self, state: str) -> None:
        """Refine the state of a turn already being shown (``WORKING`` ->
        ``RESPONDING`` / ``TOOL x``). No-op when no turn is showing: a delta or
        a tool frame that arrives outside a turn must not conjure a row."""
        if self._state is None:
            return
        self._state = state
        self._render_row()

    def end(self) -> None:
        """Hide the row — the turn is over."""
        self._state = None
        self._started_at = None
        self.display = False
        # The animation stops WITH the turn — a shine that kept running over
        # a hidden row would still be waking the event loop for nothing
        # visible, the same cost as never pausing it at all.
        if self._timer is not None:
            self._timer.pause()

    def set_behind(self, behind: "int | None") -> None:
        """How far the reader is from the newest output, or ``None`` when they
        are on it (#3712). Redraws only while a turn is showing — this rides in
        the live-turn row's spare space and does not summon one."""
        if self._behind == behind:
            return
        self._behind = behind
        if self._state is not None:
            self._render_row()

    def tick(self) -> None:
        """Advance the shine one frame and redraw (the elapsed clock rides
        along, recomputed fresh every call). No-op while hidden — reachable
        only in the gap between :meth:`end` pausing the timer and the pause
        actually taking effect, not a steady-state path."""
        if self._state is not None:
            # No modulo here: the cycle's length depends on ``len(state)``,
            # and ``_apply_shine`` is where that is known. Wrapping in both
            # places would mean two copies of the same arithmetic drifting
            # apart the first time one of them learned about a new state.
            self._shine_index += 1
            self._render_row()

    def _render_row(self) -> None:
        elapsed = None if self._started_at is None else self._clock() - self._started_at
        # The widget's own content width — the padding budget is what is
        # inside the region, not the terminal, or the hint lands past the edge
        # and comes back clipped.
        self.update(
            activity_text(
                self._state or "",
                elapsed_s=elapsed,
                width=self.content_size.width or 78,
                behind=self._behind,
                shine_index=self._shine_index if self._state is not None else None,
                colour=self._terminal_has_colour(),
            )
        )

    def _terminal_has_colour(self) -> bool:
        """Whether the shine may use its gradient rather than its fallback.

        Asked of the console every frame rather than cached at mount: the
        answer is the console's to give, and a value latched at mount would
        outlive a console that changed underneath it. The call is a plain
        attribute read.

        Defaults to ``True`` when there is no console to ask — a row mounted
        alone in a test has no app, and the interesting rendering is the one
        the operator actually gets.
        """
        try:
            return self.app.console.color_system is not None
        except Exception:
            return True
