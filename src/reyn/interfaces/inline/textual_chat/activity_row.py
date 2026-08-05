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
"""
from __future__ import annotations

import time
from typing import Callable

from textual.widgets import Static

from reyn.interfaces.inline.textual_chat import palette

#: The cancel affordance shown while a turn runs. Plain ASCII: the key it names
#: is the app's own ``ctrl+c`` binding, and this row shares a narrow terminal
#: with the queue below it.
_CANCEL_HINT = "Ctrl+C cancel"


def activity_text(
    state: str,
    *,
    elapsed_s: "float | None" = None,
    width: int = 80,
) -> str:
    """The rendered row for ``state``.

    ``elapsed_s`` is omitted entirely when ``None`` — an unknown duration
    prints no clock rather than a zero, because "00:00" reads as a fact and
    "no clock" reads as what it is.
    """
    body = f"NOW   {state}"
    if elapsed_s is not None:
        body = f"{body} {int(elapsed_s) // 60:02d}:{int(elapsed_s) % 60:02d}"
    # The hint is printed WHOLE or not at all. A clipped ``Ctrl+C cancel`` ends
    # as ``Ctrl+C``, which still reads as a complete instruction and is a
    # different one — the row would be naming a key combination nobody chose.
    # Dropping it costs nothing: the binding works whether or not it is shown.
    pad = width - len(body) - len(_CANCEL_HINT)
    if pad >= 1:
        return f"{body}{' ' * pad}{_CANCEL_HINT}"
    return body


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

    #: How often the elapsed clock is redrawn. One second: the clock has
    #: one-second resolution, so a faster tick would repaint without changing
    #: anything and a slower one would let the number lag what it claims.
    TICK_SECONDS = 1.0

    def on_mount(self) -> None:
        self.display = False
        # The clock has to advance on its OWN schedule. Without this it moved
        # only as a side effect of ``specialise`` — i.e. only while deltas were
        # arriving — so through a tool call, or after the stream ended, the row
        # kept printing a number that had stopped being true while still
        # looking live. Printing a stale duration is the same failure as
        # printing an invented one, which this row exists not to do.
        self.set_interval(self.TICK_SECONDS, self.tick)

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

    def tick(self) -> None:
        """Redraw so the elapsed clock advances. No-op while hidden."""
        if self._state is not None:
            self._render_row()

    def _render_row(self) -> None:
        elapsed = None if self._started_at is None else self._clock() - self._started_at
        # The widget's own content width — the padding budget is what is
        # inside the region, not the terminal, or the hint lands past the edge
        # and comes back clipped.
        self.update(
            activity_text(
                self._state or "", elapsed_s=elapsed, width=self.content_size.width or 78
            )
        )
