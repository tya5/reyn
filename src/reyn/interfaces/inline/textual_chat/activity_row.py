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
:data:`_SHINE_FRACTION`, :func:`_shine_ramp` and :meth:`ActivityRow.tick`.
The band was a two-character ``reverse`` when #3779 first shipped it; the
operator read that as a block blinking rather than a light moving, which is
what a two-valued band is — it has no edge to fall off. #3777 replaced it
with a cosine ramp between a theme-aware ground and peak
(:data:`palette.SHINE_GROUND_DARK`/:data:`palette.SHINE_GROUND_LIGHT` and
:data:`palette.SHINE_PEAK_DARK`/:data:`palette.SHINE_PEAK_LIGHT` — #3799
split what had been one fixed pair, since a near-white peak is invisible on
a light terminal background), keeping the old attribute band as the
no-colour fallback. The band's width is also no longer a fixed cell count:
#3799 made it :data:`_SHINE_FRACTION` of the span it crosses. One
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

from reyn.interfaces import palette
from reyn.interfaces.inline.textual_chat.sent_queue import ROW_TEXT_COLUMN

#: The cancel affordance shown while a turn runs. Plain ASCII: the key it names
#: is the app's own ``ctrl+c`` binding, and this row shares a narrow terminal
#: with the queue below it.
_CANCEL_HINT = "ctrl+c to abort"

#: The key that returns to the newest output (#3712). Named here so the row and
#: the binding that implements it cannot drift: a printed key that does not
#: work is worse than none, and the conversation pane's own ``end``/``G`` only
#: fire while it holds focus — i.e. never from where the reader actually is.
LATEST_HINT = "ctrl+end to latest"

#: How wide the band is, as a FRACTION of what it travels across. Not a fixed
#: character count: #3777 stage 2 widened the band's runway from the state word
#: to the whole row, and a width in cells that read as a moving point across
#: ten characters reads as a speck across eighty. A fraction keeps the band the
#: same size relative to what it is crossing, which is what "looks the same"
#: actually means here. 40% is the proportion the common CSS shimmer
#: implementations use.
_SHINE_FRACTION = 0.4

#: How long ONE pass takes, in seconds — the pace is a duration, not a speed.
#: A speed in cells per second was the earlier shape and it does not survive a
#: change of runway: the same cells/second crosses a short row quickly and a
#: long one slowly, so widening the band's span silently changed how urgent the
#: row felt. Fixing the DURATION makes the row feel the same at any width,
#: which is the property being aimed at.
_SHINE_PASS_SECONDS = 3.0

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



@lru_cache(maxsize=64)
def _shine_ramp(half: int, ground: str, peak: str) -> "tuple[str, ...]":
    """One colour per cell of the band, brightest first, ending at ``ground``.

    Index is the cell's DISTANCE from the band's centre, so ``[0]`` is the peak
    and the last entry IS the ground — the band's outermost cell is already the
    colour every other character on the row is wearing, so there is no step
    where the band ends. That is the whole correction: painting only the band
    and leaving the rest at the terminal's own foreground put a dark cell hard
    against an undimmed ground at each end, and the operator read the result as
    three things moving rather than one.

    The shape is a raised cosine — ``0.5 + cos(pi * d / half) / 2`` — reached by
    calling rich's public ``blend_rgb`` rather than copying its
    ``_get_pulse_segments``. rich's ``ProgressBar`` is no use to us: it draws
    its OWN bar, it does not light up someone else's text.

    Cached across half-widths and ground pairs, since the row's width and the
    terminal's theme both change rarely and this runs on every frame.
    """
    dim = Color.parse(ground).get_truecolor()
    bright = Color.parse(peak).get_truecolor()
    ramp: "list[str]" = []
    for distance in range(half + 1):
        fade = 1.0 if half == 0 else 0.5 + math.cos(math.pi * distance / half) / 2.0
        blended = blend_rgb(dim, bright, cross_fade=fade)
        ramp.append(f"#{blended.red:02x}{blended.green:02x}{blended.blue:02x}")
    return tuple(ramp)


def activity_text(
    state: str,
    *,
    elapsed_s: "float | None" = None,
    width: int = 80,
    entries: int = 0,
    away: bool = False,
    shine_phase: "float | None" = None,
    colour: bool = True,
    dark: bool = True,
) -> Content:
    """The rendered row for ``state``.

    ``elapsed_s`` is omitted entirely when ``None`` — an unknown duration
    prints no clock rather than a zero, because "0s" reads as a fact — a turn
    that just started — and "no clock" reads as what it is: a turn whose start
    this client never saw.

    The format changes at a minute (#3777, owner ruling): bare seconds below
    (``12s``, from the owner's mock), ``MM:SS`` at and above it (``20:47``).
    Each is the readable one in its own range — ``00:12`` makes a short turn
    look like a stopwatch, and ``1247s`` is a number nobody reads at a glance.

    A format that changes under the operator is normally this module deciding
    something that is theirs; the reason it is allowed here is that **what the
    reader is looking at is the elapsed time, not the format**, so a change
    toward the readable form is not something they have to notice or learn.
    That argument is what the ruling turned on, and it is the thing to re-check
    before extending this to any other switching format.

    ``entries`` is how many entries this TURN has produced, counted from the
    moment it started and shown from zero upward. ``away`` is whether the
    reader has scrolled off the newest output. They are two parameters because
    they are two facts: the previous shape was one ``behind: int | None`` where
    ``None`` meant "following" and an int meant "away, by this much", which
    made the count's baseline *the instant the reader scrolled away* — an event
    the reader never sees, so ``LIVE +N`` offered no occasion on which its
    meaning could be learned. A turn's start is something the reader did see.
    ``away`` now decides one thing only: whether the return-to-latest hint is
    printed.

    ``shine_phase`` is where in one pass the travelling highlight is, as a
    fraction in ``[0, 1)`` — ``None`` paints no band (a static row, e.g. while
    no turn is showing). ``colour`` is whether the terminal can show one, and
    ``dark`` whether its background is dark; between them they choose the
    ground/peak pair, or the attribute fallback when there is no colour at all.

    Segments are separated by ``·`` and read left to right, hints included:
    there is no right-aligned slot any more. A right edge only reads as a
    place things belong when the row is wide enough for the gap to look
    deliberate, and this row shares a terminal with everything else.
    """
    prefix = " " * ROW_TEXT_COLUMN if state else ""
    head = f"{prefix}{state}"
    if elapsed_s is not None:
        seconds = int(elapsed_s)
        head = (
            f"{head} {seconds}s" if seconds < 60
            else f"{head} {seconds // 60:02d}:{seconds % 60:02d}"
        )

    # Segments in the order they are printed, each with what it costs to lose.
    # Dropping from the right would take the abort hint first, and a row that
    # gave up "how to stop this" to keep a count would have its priorities
    # backwards. The head is never dropped: a row that cannot say what is
    # happening has nothing left to be.
    optional = [
        (f"{entries} entries", 0),
        (LATEST_HINT, 2) if away else None,
        (_CANCEL_HINT, 3),
    ]
    segments = [seg for seg in optional if seg is not None]
    while segments and len(" · ".join([head, *(t for t, _ in segments)])) > width:
        segments.remove(min(segments, key=lambda seg: seg[1]))
    line = " · ".join([head, *(text for text, _ in segments)])

    content = Content(line)
    if shine_phase is not None and state:
        # The whole row (#3777, owner call). #3779 confined the band to
        # ``state`` so it could not wander onto the clock or the hint; with the
        # glyph gone and the row reading as ONE object, the owner asked for the
        # light to cross all of it.
        content = _apply_shine(content, len(line), shine_phase, colour, dark)
    return content


def _apply_shine(
    content: Content, span: int, phase: float, colour: bool, dark: bool
) -> Content:
    """The travelling band over ``span`` characters, at ``phase`` of one pass.

    ``phase`` is a fraction in ``[0, 1)`` rather than a frame number, so the
    caller expresses WHEN in the pass the row is, and the pass takes the same
    wall-clock time whatever the row's width. A frame counter cannot do that:
    its cycle is measured in cells, so a wider row takes proportionally longer
    and the row silently feels less urgent the more there is on it.

    EVERY character is painted, not only the band. Outside the band each cell
    takes the ground, and the band's own outermost cell IS the ground, so the
    band has no edge to step off. Painting only the band and leaving the rest
    at the terminal's foreground is what made the operator see three moving
    things rather than one — a dark cell at each end of the band, hard against
    an undimmed ground, is two more features than the design has.

    The centre runs from ``-half`` to ``span - 1 + half`` so the light enters
    and leaves rather than materialising at the first character and vanishing
    at the last.
    """
    ground = palette.SHINE_GROUND_DARK if dark else palette.SHINE_GROUND_LIGHT
    peak = palette.SHINE_PEAK_DARK if dark else palette.SHINE_PEAK_LIGHT
    half = max(1, round(span * _SHINE_FRACTION / 2))
    if not colour:
        # Same two characters #3779 shipped, so the degraded form is one this
        # row has already been seen in rather than a third design. No ground:
        # an attribute has no "slightly on", so there is nothing to paint the
        # rest of the row WITH that would not simply emphasise all of it.
        start = max(0, min(int(phase * span), span - 1))
        return content.stylize(
            _SHINE_FALLBACK_STYLE, start, min(start + _SHINE_FALLBACK_WIDTH, span)
        )
    ramp = _shine_ramp(half, ground, peak)
    centre = phase * (span + 2 * half) - half
    for position in range(span):
        distance = round(abs(position - centre))
        style = ramp[distance] if distance <= half else ground
        content = content.stylize(style, position, position + 1)
    return content


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
        self._entries = 0
        self._away = False
        self._timer: "Timer | None" = None

    #: The shine's frame rate (owner-picked design "A"). Also what drives the
    #: elapsed clock now — ONE timer, not two: :meth:`tick` recomputes the
    #: elapsed seconds from the real clock on every call regardless of how
    #: often it runs, so a faster shared tick costs nothing in correctness,
    #: only one Static line of extra string work per frame
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
            # The count's baseline is the turn, so it is zeroed HERE and
            # nowhere else. Zeroing it on a scroll edge is what made the old
            # ``LIVE +N`` unreadable: its baseline was an event the reader
            # could not observe.
            self._entries = 0
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

    def set_entries(self, entries: int) -> None:
        """How many entries this turn has produced so far (#3777).

        Independent of where the reader is: the count answers "how much has
        happened", and the answer does not change because somebody scrolled.
        Redraws only while a turn is showing — this rides in the live-turn
        row and does not summon one.
        """
        if self._entries == entries:
            return
        self._entries = entries
        if self._state is not None:
            self._render_row()

    def set_away(self, away: bool) -> None:
        """Whether the reader has scrolled off the newest output (#3712).

        Decides ONE thing: whether the return-to-latest hint is printed. It is
        deliberately not an input to the count — conflating the two is what the
        previous ``behind: int | None`` did, and re-reading that single value
        under a new name would have carried the same defect forward.
        """
        if self._away == away:
            return
        self._away = away
        if self._state is not None:
            self._render_row()

    def tick(self) -> None:
        """Redraw one frame: the shine advances and the elapsed clock rides
        along, recomputed fresh from the real clock every call. No-op while
        hidden — reachable only in the gap between :meth:`end` pausing the
        timer and the pause actually taking effect, not a steady-state path.

        The shine's position is derived from the CLOCK, not from a counter this
        method increments. A counter measures the pass in frames, so a dropped
        or delayed tick shortens the pass and a wider row lengthens it; reading
        the clock makes one pass take :data:`_SHINE_PASS_SECONDS` whatever the
        renderer and the terminal happen to be doing.
        """
        if self._state is not None:
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
                entries=self._entries,
                away=self._away,
                shine_phase=(
                    None if self._state is None
                    else (self._clock() % _SHINE_PASS_SECONDS) / _SHINE_PASS_SECONDS
                ),
                colour=self._terminal_has_colour(),
                dark=self._terminal_is_dark(),
            )
        )

    def _terminal_is_dark(self) -> bool:
        """Whether the terminal's background is dark, so the shine can pick the
        ground/peak pair that is actually visible against it.

        One pair cannot serve both: the dark pair's peak is nearly white, which
        against a white terminal is the band disappearing rather than the band
        being subtle. Asked of the theme every frame rather than cached, for
        the same reason as :meth:`_terminal_has_colour` — the answer is the
        app's to give, and a value latched at mount would outlive a theme
        change.

        Defaults to dark when there is no app to ask (a row mounted alone in a
        test), which is the terminal reyn is most often run in.
        """
        try:
            return bool(self.app.current_theme.dark)
        except Exception:
            return True

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
