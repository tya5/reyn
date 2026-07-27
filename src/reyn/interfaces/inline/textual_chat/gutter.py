"""State-coloured LEFT gutter + elapsed-time RIGHT gutter for the Textual chat
surface's flowview.

:class:`ReynGutter` fills the flowview LEFT gutter column with a kind-driven
glyph (via :func:`_gutter_glyph_color`) whose COLOUR is driven by the entry's
:class:`~textual_flowview.EntryState` (:data:`_STATE_COLOR`); a ``RUNNING`` entry
BLINKS through :data:`_RUNNING_FRAMES`, the frame selected from a monotonic clock
(``int(clock() / frame_period)``). The blink is TIME-based: ``decorate`` reads the
clock itself, and textual-flowview's own ``FlowView(animation_fps=N)`` re-invokes
the decorator on each animation tick so the glyph advances with wall time — no
app-held frame counter, no app-side timer. textual-flowview is never modified or
forked.

:class:`ReynTimingGutter` (Phase ④, #3283) fills the flowview RIGHT gutter
column (``right_decorator``/``right_gutter_width``, additive flowview params)
with a per-entry elapsed-time label — see its own docstring for the content-set
decision (elapsed only; cost/token and a dedicated state chip were both
evaluated and dropped) and the live-vs-restore split.

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

from ._meta_keys import ELAPSED_SECS_KEY as _ELAPSED_SECS_KEY
from ._meta_keys import RUNNING_SINCE_KEY as _RUNNING_SINCE_KEY

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual_flowview import Entry

    from reyn.runtime.outbox import OutboxMessage

# EntryState → gutter colour (Phase 2 state-color gutter). The CC state
# palette: RUNNING amber, SUCCESS green, ERROR coral. DEFAULT has NO entry
# here by design: :meth:`ReynGutter.decorate` handles DEFAULT with its own
# dedicated branch that falls back to the entry's KIND colour (``kind_color``
# from :func:`_gutter_glyph_color`), because different kinds need different
# DEFAULT-state colours (an ordinary user/agent row vs. a resolved
# intervention, #3324 — see that function's "intervention" branch for how a
# resolved intervention gets a non-amber colour without leaving DEFAULT).
# A single scalar here would force every DEFAULT row to the same colour,
# which is wrong. CANCELLED still maps to dim (no per-kind distinction
# needed for it).
_STATE_COLOR: "dict[EntryState, str]" = {
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
    if kind == "intervention":
        # #3299 P2 §5: an intervention's flow entry stays EntryState.DEFAULT in
        # BOTH the pending and resolved cases (never RUNNING/SUCCESS/ERROR — an
        # answer is neither an outcome to celebrate nor a failure, #3296). With
        # the state fixed at DEFAULT, the gutter's kind colour is the only axis
        # left to distinguish the two, so both legs are special-cased here
        # rather than falling through to ``_KIND_LINE["intervention"]``'s
        # amber ("needs you") colour:
        if not (msg.meta or {}).get("_answer_label"):
            # PENDING: a dim "awaiting" marker instead of the kind's normal
            # amber glyph.
            return "⋯", _CC_DIM
        # RESOLVED (#3324): reusing the amber kind colour here made a resolved
        # intervention indistinguishable from one still awaiting an answer —
        # both rendered the same "◆ needs you" amber, because DEFAULT-state
        # entries fall back to their kind colour and "intervention"'s kind
        # colour IS that amber. Keep the kind glyph (``◆``) but swap in
        # ``_CC_DONE`` — the same green already used for the row's own
        # "✓ answered: <label>" body line (``ReynPresenter.
        # _present_intervention_pending``) — so pending (dim ⋯), resolved
        # (green ◆) and an ordinary DEFAULT row (its own kind colour, e.g.
        # plain text for user/agent) are three mutually distinct renders.
        glyph = _KIND_LINE["intervention"][0].strip()[:1]
        return glyph, _CC_DONE
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


def _format_elapsed(seconds: float) -> str:
    """A compact elapsed-time label — ``Ns`` / ``Nm`` / ``Nh`` — bounded to at
    most 3 characters of digits+unit so :data:`RIGHT_GUTTER_WIDTH` can stay
    narrow. Used by :class:`ReynTimingGutter` for both the LIVE value (read off
    the clock every repaint) and the SETTLED value (a single stashed int)."""
    secs = max(0, int(seconds))
    if secs < 100:
        return f"{secs}s"
    minutes = secs // 60
    if minutes < 100:
        return f"{minutes}m"
    return f"{minutes // 60}h"


#: FlowView RIGHT-gutter column width (Phase ④, #3283) — wide enough for
#: :func:`_format_elapsed`'s longest label (3 characters, e.g. ``"99s"``)
#: plus one column of breathing room. Wired into ``app.py``'s
#: ``FlowView(right_gutter_width=…)`` config.
RIGHT_GUTTER_WIDTH = 4


class ReynTimingGutter:
    """Fills the flowview RIGHT gutter with a per-entry ELAPSED-TIME label
    (Phase ④, #3283) — the right-gutter half of the #3283 spec's "left gutter
    keeps state, right gutter shows per-entry metadata" split. The LEFT gutter
    (:class:`ReynGutter`) is untouched by this class.

    **Content set — elapsed time only.** The umbrella issue listed turn
    cost/tokens and a state chip as CANDIDATES; both were evaluated against
    what data actually exists and dropped (owner-adjudicated on #3283):

    - **Turn cost / tokens**: ``BudgetTracker`` (``reyn.runtime.budget``) is
      CUMULATIVE ONLY — per-agent / daily / monthly totals. There is no
      per-turn or per-entry cost/token field anywhere in the runtime.
      Showing the running total on one arbitrary entry, or a value obtained
      by dividing it, would be a FABRICATED per-entry number — not shown.
    - **A dedicated state chip**: the left gutter already fully encodes
      :class:`~textual_flowview.EntryState` via glyph + colour (#3273's
      contract); a right-side chip would duplicate that same axis for no
      new information — not added.

    **Only entries that HAVE elapsed data show it** — the negative control.
    A ``tool_call_started`` entry shows a label when it is either:

    - currently RUNNING — the LIVE value, read off :data:`_RUNNING_SINCE_KEY`
      and the injected clock on every repaint (matches the body's live
      ``elapsed Ns`` indicator, :mod:`.presenter`); or
    - SETTLED with a captured final duration — :data:`_ELAPSED_SECS_KEY`,
      stamped once by ``app.py`` at settle time (``_coalesce_tool_result`` /
      ``_sweep_orphaned_running_tools``).

    Every other entry — user lines, agent replies, interventions, and ANY
    RESTORED row (elapsed is LIVE-SESSION ONLY BY DECISION — a persisted
    ``ChatMessage`` carries no timing field at all; see
    :data:`_ELAPSED_SECS_KEY`'s docstring for why that is a decision, not an
    oversight) — renders an EMPTY right-gutter cell: no placeholder, no
    ``"0s"``, nothing carried over from a neighbouring entry.

    ``clock`` is injectable (default :func:`time.monotonic`), mirroring
    :class:`ReynGutter`, so a test can drive the live value deterministically.
    """

    def __init__(self, *, clock: "Callable[[], float]" = time.monotonic) -> None:
        self._clock = clock

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        meta = entry.item.meta or {}
        since = meta.get(_RUNNING_SINCE_KEY)
        if isinstance(since, (int, float)):
            label = _format_elapsed(self._clock() - since)
        else:
            final = meta.get(_ELAPSED_SECS_KEY)
            label = _format_elapsed(final) if isinstance(final, (int, float)) else ""
        return Text(label.rjust(width), style=_CC_DIM)
