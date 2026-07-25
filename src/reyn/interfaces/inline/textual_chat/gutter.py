"""State-coloured gutter marker for the Textual chat surface's flowview.

:class:`ReynGutter` fills the flowview gutter column with a kind-driven glyph
(via :func:`_gutter_glyph_color`) whose COLOUR is driven by the entry's
:class:`~textual_flowview.EntryState` (:data:`_STATE_COLOR`); a ``RUNNING`` entry
BLINKS through :data:`_RUNNING_FRAMES` selected by a shared app-side counter. The
blink lives ENTIRELY in reyn (counter + timer in the app, frame selection here);
textual-flowview is never modified or forked.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual_flowview`` import never
reaches an always-loaded module.
"""
from __future__ import annotations

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

# Running-blink frames: a two-phase ●/○ pulse cycled by the app-side timer's
# shared frame counter (:meth:`TextualChatApp._advance_blink`). The blink lives
# ENTIRELY in reyn — the counter + timer in the app, the frame selection here;
# textual-flowview is never modified or forked.
_RUNNING_FRAMES = ("●", "○")


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
    :data:`_RUNNING_FRAMES` selected by ``blink_frame()`` — a shared counter
    advanced by the app-side timer (:meth:`TextualChatApp._advance_blink`). The
    decorator only READS the counter; the timer, the counter, and the redraw
    trigger all live in the reyn app. textual-flowview is never modified.
    ``decorate`` stays synchronous + cheap (it runs on every gutter repaint)."""

    def __init__(self, blink_frame: "Callable[[], int]" = lambda: 0) -> None:
        self._blink_frame = blink_frame

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        glyph, kind_color = _gutter_glyph_color(entry.item)
        state = entry.state
        if state is EntryState.RUNNING:
            glyph = _RUNNING_FRAMES[self._blink_frame() % len(_RUNNING_FRAMES)]
            color = _CC_WARN
        elif state is EntryState.DEFAULT:
            color = kind_color
        else:
            color = _STATE_COLOR.get(state, kind_color)
        return Text(glyph.ljust(width), style=color)
