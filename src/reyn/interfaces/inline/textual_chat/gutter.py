"""State-coloured gutter marker for the Textual chat surface's flowview.

:class:`ReynGutter` fills the flowview gutter column with a kind-driven glyph
(via :func:`_gutter_glyph_color`) whose COLOUR is driven by the entry's
:class:`~textual_flowview.EntryState` (:data:`_STATE_COLOR`); a ``RUNNING`` entry
BLINKS through :data:`_RUNNING_FRAMES`, the frame selected from a monotonic clock
(``int(clock() / frame_period)``). The blink is TIME-based: ``decorate`` reads the
clock itself, and textual-flowview's own ``FlowView(animation_fps=N)`` re-invokes
the decorator on each animation tick so the glyph advances with wall time — no
app-held frame counter, no app-side timer. textual-flowview is never modified or
forked.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual_flowview`` import never
reaches an always-loaded module.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual_flowview import EntryState

from reyn.interfaces.repl.renderer import (
    _CC_DIM,
    _CC_DONE,
    _CC_ERR,
    _CC_TEXT,
    _CC_WARN,
    _KIND_LINE,
)

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual_flowview import Entry

    from reyn.runtime.outbox import OutboxMessage

# EntryState → gutter colour (Phase 2 state-color gutter). The CC state
# palette: RUNNING amber, SUCCESS green, ERROR coral, DEFAULT/CANCELLED dim.
# Applied by :meth:`ReynGutter.decorate` when an entry carries a non-DEFAULT
# lifecycle state; DEFAULT entries fall back to their kind colour.
_STATE_COLOR: "dict[EntryState, str]" = {
    EntryState.DEFAULT: _CC_DIM,
    EntryState.RUNNING: _CC_WARN,
    EntryState.SUCCESS: _CC_DONE,
    EntryState.ERROR: _CC_ERR,
    EntryState.CANCELLED: _CC_DIM,
}

# Running-blink frames: a two-phase ●/○ pulse. The frame is picked from a
# monotonic clock (``int(clock() / frame_period) % len(_RUNNING_FRAMES)``) in
# :meth:`ReynGutter.decorate`; textual-flowview's ``FlowView(animation_fps=N)``
# re-invokes the decorator each animation tick, so the pulse advances with wall
# time. No app-side timer, no shared counter — the blink lives in this decorator
# + the library's native animation clock; textual-flowview is never modified.
_RUNNING_FRAMES = ("●", "○")

#: Seconds each running-blink frame is held before the next glyph. The visible
#: blink cadence (== the pre-native app-side timer's 0.5s interval); paired with
#: ``FlowView(animation_fps=1/_RUNNING_FRAME_PERIOD)`` so the decorator is
#: re-invoked at least once per frame. ``<= 0`` freezes the blink (static frame 0).
_RUNNING_FRAME_PERIOD = 0.5


def _gutter_glyph_color(msg: "OutboxMessage") -> "tuple[str, str]":
    """The gutter glyph + kind-colour for one display frame, keyed off ``_KIND_LINE``.

    Mirrors the plain renderer's marker column: the ``_KIND_LINE`` glyph (its
    leading non-space char) for message-y kinds, the ``●`` tool-header /  ``⎿``
    tool-result markers otherwise. The colour returned here is the KIND colour;
    a non-DEFAULT :class:`EntryState` overrides it in :meth:`ReynGutter.decorate`
    (state-driven colour, Phase 2). Kept cheap — ``decorate`` runs on every repaint.
    """
    kind = msg.kind
    if kind == "tool_call_started":
        return "●", _CC_TEXT
    if kind == "tool_call_completed":
        return "⎿", _CC_DIM
    if kind == "tool_call_failed":
        return "⎿", _CC_ERR
    line = _KIND_LINE.get(kind)
    if line is None:
        return "", _CC_DIM
    glyph = line[0].strip()[:1]
    return glyph, line[1]


class ReynGutter:
    """Fills the flowview gutter column with a STATE-COLOURED marker (Phase 2).

    The glyph is kind-driven (``❯`` user, ``●`` assistant / tool-header, ``⎿``
    tool-result — via :func:`_gutter_glyph_color`); the COLOUR is driven by the
    entry's :class:`EntryState`: RUNNING amber, SUCCESS green, ERROR coral
    (:data:`_STATE_COLOR`). A DEFAULT-state entry keeps its kind colour, so plain
    message rows are unchanged from Phase 1.

    While an entry is ``RUNNING`` its marker BLINKS: the glyph cycles through
    :data:`_RUNNING_FRAMES`, the frame picked from a monotonic clock
    (``int(clock() / frame_period) % len(_RUNNING_FRAMES)``). The blink is
    TIME-based — ``decorate`` reads the clock itself and returns the current
    frame; the REDRAW that advances it is textual-flowview's native
    ``FlowView(animation_fps=N)`` tick, which re-invokes this decorator on each
    animation frame. No app-side timer, no shared counter. textual-flowview is
    never modified. ``decorate`` stays synchronous + cheap (it runs on every
    gutter repaint).

    ``clock`` is injectable (default :func:`time.monotonic`) so a test can drive
    the frame deterministically; ``frame_period <= 0`` freezes the blink to a
    static frame 0 (the animation is additive — a frozen clock leaves a correct,
    non-animated amber gutter)."""

    def __init__(
        self,
        *,
        frame_period: float = _RUNNING_FRAME_PERIOD,
        clock: "Callable[[], float]" = time.monotonic,
    ) -> None:
        self._frame_period = frame_period
        self._clock = clock

    def _running_frame(self) -> str:
        """The current ``_RUNNING_FRAMES`` glyph, selected from the clock. A
        non-positive ``frame_period`` freezes to frame 0 (animation neutered)."""
        if self._frame_period <= 0:
            return _RUNNING_FRAMES[0]
        idx = int(self._clock() / self._frame_period) % len(_RUNNING_FRAMES)
        return _RUNNING_FRAMES[idx]

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        glyph, kind_color = _gutter_glyph_color(entry.item)
        state = entry.state
        if state is EntryState.RUNNING:
            glyph = self._running_frame()
            color = _CC_WARN
        elif state is EntryState.DEFAULT:
            color = kind_color
        else:
            color = _STATE_COLOR.get(state, kind_color)
        return Text(glyph.ljust(width), style=color)
