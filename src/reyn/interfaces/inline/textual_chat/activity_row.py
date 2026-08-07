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
itself now carries a travelling ``reverse`` highlight ("shine", design "A" of
three text-mockup options put to the owner) while a turn runs — see
:data:`_SHINE_WIDTH`/:data:`_SHINE_STYLE` and :meth:`ActivityRow.tick`. One
timer drives both the shine and the elapsed clock (the clock is recomputed
fresh from the real clock on every tick regardless of rate, so sharing the
timer costs nothing in correctness) and is paused/resumed by
:meth:`ActivityRow.end`/:meth:`begin` rather than left running while hidden
— an animation ticking over an idle ssh session for a row nobody sees is
exactly the cost that split exists to avoid.

A later owner call (#3777, same day) put a small piece of that vacated
column budget back: the state word now opens with :data:`_STATE_GLYPH`
(``"▶ "``), the filled counterpart of the sent-queue's unfilled
:data:`~reyn.interfaces.inline.textual_chat.sent_queue._QUEUED_GLYPH`
(``"▷"``) — a queued item's glyph and this row's glyph are the SAME shape,
one hollow and one filled, so promotion reads as the shape filling in
rather than as one icon replacing an unrelated one. See
:func:`activity_text`'s docstring for the shine-clearance argument.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from textual.content import Content
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.timer import Timer

from reyn.interfaces.inline.textual_chat import palette

#: The cancel affordance shown while a turn runs. Plain ASCII: the key it names
#: is the app's own ``ctrl+c`` binding, and this row shares a narrow terminal
#: with the queue below it.
_CANCEL_HINT = "Ctrl+C cancel"

#: The key that returns to the newest output (#3712). Named here so the row and
#: the binding that implements it cannot drift: a printed key that does not
#: work is worse than none, and the conversation pane's own ``end``/``G`` only
#: fire while it holds focus — i.e. never from where the reader actually is.
LATEST_HINT = "Ctrl+End"

#: How wide the travelling highlight band is, in characters. 2: narrow enough
#: to read as a POINT of light moving rather than a block of emphasis, wide
#: enough to be visible at 6fps without needing sub-cell rendering.
_SHINE_WIDTH = 2

#: The shine's SGR attribute. Not a ``palette.py`` marker: that module's
#: at-sign-wrapped markers resolve inside a CSS ``DEFAULT_CSS`` string via
#: :func:`palette.css`; this is applied at RUNTIME to a narrow content span
#: via ``Content.stylize``, the same non-CSS style-constant shape
#: ``gutter.py``'s own glyph colours already use. ``reverse`` rather than a
#: background: applied to only ``_SHINE_WIDTH`` characters at a time, the
#: same content-scale emphasis #3490 already established for a moving mark
#: (a FULL-row inversion was rejected there, not a narrow travelling one)
#: — and an SGR attribute, not a colour, survives every ansi theme the way
#: a background value would not.
_SHINE_STYLE = "reverse"

#: The NOW-row glyph (#3777, owner call: a play-family mark — filled, to
#: read as "running", pairing with
#: :data:`~reyn.interfaces.inline.textual_chat.sent_queue._QUEUED_GLYPH`'s
#: unfilled counterpart on the queue row so a promoted item shows the SAME
#: shape it had a moment ago, only filled in — the "connection" the owner
#: asked for. Reoccupies part of the column budget #3779 vacated when it
#: dropped the 6-column ``NOW   `` label; the glyph earns that back by
#: carrying real information (running, not a static word) rather than
#: repeating "NOW" as a caption. ``wcwidth`` confirmed single-column and a
#: full-repo grep found no prior use before this landed (see #3777).
_STATE_GLYPH = "▶"


def activity_text(
    state: str,
    *,
    elapsed_s: "float | None" = None,
    width: int = 80,
    behind: "int | None" = None,
    shine_index: "int | None" = None,
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
    label): a ``_SHINE_WIDTH``-character ``reverse`` band travelling through
    ``state`` ITSELF, at the given character offset — ``None`` paints no
    band (a static row, e.g. while no turn is showing). The band is confined
    to ``state``'s own span, never the elapsed clock, the leading
    :data:`_STATE_GLYPH`, or the right-aligned hint — those are different
    information (or, for the glyph, a different piece of state entirely) and
    stay plain. #3779 vacated the old 6-column ``NOW   `` slot; #3777 gives
    ``state`` a 2-column ``"▶ "`` prefix instead — smaller than the old
    label, and unlike it, informative on its own (see :data:`_STATE_GLYPH`'s
    module comment for why that column cost is worth paying again).
    """
    prefix = f"{_STATE_GLYPH} " if state else ""
    body = f"{prefix}{state}"
    if elapsed_s is not None:
        body = f"{body} {int(elapsed_s) // 60:02d}:{int(elapsed_s) % 60:02d}"
    suffix = f"LIVE +{behind} · {LATEST_HINT} latest" if behind else _CANCEL_HINT
    content = _with_suffix(body, suffix, width)
    if shine_index is not None and state:
        # Offsets are into ``state`` alone; ``prefix`` shifts them right by
        # its own length so the band never reaches back over the glyph or
        # the space after it — the clearance #3777 required.
        start = len(prefix) + max(0, min(shine_index, len(state) - 1))
        end = min(start + _SHINE_WIDTH, len(prefix) + len(state))
        content = content.stylize(_SHINE_STYLE, start, end)
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
            self._shine_index = (self._shine_index + 1) % max(1, len(self._state))
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
            )
        )
