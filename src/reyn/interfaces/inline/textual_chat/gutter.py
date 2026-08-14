"""State-coloured LEFT gutter + elapsed/turn-token RIGHT gutter for the Textual
chat surface's flowview.

:class:`ReynGutter` fills the flowview LEFT gutter column with a kind-driven
glyph (via :func:`_gutter_glyph_color`) whose COLOUR is driven by the entry's
:class:`~textual_flowview.EntryState` (:data:`_STATE_COLOR`); a ``RUNNING`` entry
BLINKS through :data:`_RUNNING_FRAMES`, the frame selected from a monotonic clock
(``int(clock() / frame_period)``). The blink is TIME-based: ``decorate`` reads the
clock itself, and textual-flowview's own ``FlowView(animation_fps=N)`` re-invokes
the decorator on each animation tick so the glyph advances with wall time — no
app-held frame counter, no app-side timer. textual-flowview is never modified or
forked.

:class:`ReynRightGutter` (Phase ④, #3283) fills the flowview RIGHT gutter column
(``right_decorator``/``right_gutter_width``, additive flowview params). It is a
composite of two single-purpose label producers, since flowview takes exactly one
right decorator:

- :class:`ReynTimingGutter` — per-entry ELAPSED time (live off the clock while a
  tool row runs, the captured final value once it settles);
- :class:`ReynTurnUsageGutter` — the row's turn's real TOKEN COUNT, read through
  an injected keyed lookup over ``BudgetTracker``'s per-turn buckets
  (#3339/#3342). Unknown or evicted turns render :data:`TURN_USAGE_UNKNOWN`
  (``"—"``), kept distinct from a real ``0``, and never a figure derived by
  differencing cumulative counters. The lookup also returns the turn's USD cost;
  this column deliberately does not display it (owner call — see the class).

See each class's docstring for its content-set decision and its live-vs-restore
posture (neither elapsed nor tokens survive a restart — both render honestly
absent rather than reconstructed). A dedicated state chip, the umbrella issue's
third right-gutter candidate, stays dropped: the LEFT gutter already encodes
``EntryState``.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual_flowview`` import never
reaches an always-loaded module.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from rich.cells import cell_len
from rich.text import Text
from textual_flowview import EntryState

from reyn.interfaces.repl.renderer import (
    _CC_AMBIENT,
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
    from reyn.tools import ToolRegistry

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

#: The ADDRESSED-entry rail (#3490): a thin bar marking the keyboard cursor's
#: row — which is also where search puts the cursor (#3493), so there is one
#: addressed row and never two marks. One cell wide.
#:
#: Drawn in the RIGHT gutter's LEADING cell (#3526, owner directive
#: "flowview ハイライトの左ガターのラインだけど、右ガターのラインに変更して").
#: It was previously the LEFT gutter's TRAILING cell, and that position was
#: load-bearing for two reasons — the record of what the move keeps and what
#: it gives up:
#:
#: - **costs no body column** — KEPT. The right gutter is a fixed
#:   :data:`RIGHT_GUTTER_WIDTH` column band, so spending its leading cell takes
#:   nothing from the body, exactly as the left gutter's trailing cell did.
#: - **doubles as the gutter/body separator on the marked row** — CHANGED, not
#:   lost. On the left it divided the state glyph from the body; on the right it
#:   divides the body from the elapsed/token labels. Same job, other edge. What
#:   genuinely differs is DISTANCE from the text: the left rail sat against the
#:   start of every line, while the right rail sits at the body's right margin,
#:   which most lines stop well short of. That is inherent to the side the owner
#:   asked for and is why the glyph hugs the body-facing edge of its cell.
#:
#: ``▏`` (U+258F, LEFT ONE EIGHTH BLOCK) rather than the former ``▎``
#: (U+258E, three-eighths): both hug the left edge of their cell, which on the
#: right gutter is the edge FACING the body, so the bar stays adjacent to the
#: text instead of drifting toward the labels. The thinner weight is deliberate
#: — at the body's ragged right margin a heavier bar reads as a border around
#: the pane rather than as a marker on a row.
_MARK_RAIL = "▏"

#: The rail's colour. A NAMED ANSI colour, not one of the ``_CC_*`` hex
#: constants, and that is the whole point (#3493, owner directive "ターミナルの
#: テーマ参照できるならそっち優先"): ``"blue"`` emits the palette-relative
#: ``SGR 34``, which the TERMINAL resolves from its own theme, so the rail
#: follows a light or dark terminal automatically. A truecolor hex emits
#: ``38;2;r;g;b`` and looks identical on every theme — fine for content the
#: palette owns, wrong for a bare position marker that has to sit legibly on
#: whatever background the user chose. (``"default"`` — the terminal's plain
#: foreground, as ``_CC_TEXT`` uses — is the even-more-neutral alternative;
#: a palette blue is kept so the rail reads as an accent distinct from the
#: state glyph beside it rather than as more text.) Being addressed is a
#: POSITION, so it must not borrow the ``_STATE_COLOR`` vocabulary either.
_MARK_COLOR = _CC_TEXT

# #3329: retrieval-demotion. A tool call's op-class taxonomy is NOT hardcoded
# here as a name list (the #3273 deferred-track incident this issue names
# twice: "手動列挙は次も漏れる") — it is DERIVED from the existing, complete
# ``ToolDefinition.purity`` axis already declared per tool in ``reyn.tools``
# (measured #3329: all 76 registered tools declare ``purity`` explicitly;
# no tool relies on the dataclass default). ``purity="read_only"`` is the
# authoritative "取得系" signal this table's left column names.
#
# One explicit exemption, not an accident of today's values (lead-coder
# ruling, #3329): every dynamically-installed MCP server tool dispatches
# through ONE of these two fixed wrapper tools (``mcp_tool_name``/
# ``<server>__<tool>`` is an ARGUMENT, never the invoked function name — see
# ``dispatcher.py``'s ``tool_called`` event, whose ``tool`` field is always
# the wrapper's own name) — so no INDIVIDUAL MCP tool's real read/write
# behaviour is knowable from ``purity`` here; the wrapper's own
# ``purity="side_effect"`` already keeps this case un-demoted, but is
# written explicitly below so that stays true ON PURPOSE, not by
# coincidence if either wrapper's declared purity ever changes.
_MCP_DYNAMIC_DISPATCH_EXEMPT: "frozenset[str]" = frozenset({
    "call_mcp_tool", "mcp_call_tool",
})


def _default_tool_registry() -> "ToolRegistry":
    """The default :class:`ToolRegistry`, built once and cached.

    :func:`reyn.tools.get_default_registry` documents itself as a fresh,
    lightweight construction callers may cache — :func:`_gutter_glyph_color`
    runs on every gutter repaint (its own docstring's constraint), so this
    module builds it once rather than on every frame."""
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        from reyn.tools import get_default_registry
        _TOOL_REGISTRY = get_default_registry()
    return _TOOL_REGISTRY


_TOOL_REGISTRY: "ToolRegistry | None" = None


def _is_retrieval_tool(tool_name: str) -> bool:
    """True iff *tool_name* is a "取得系" (retrieval) tool for #3329's gutter
    demotion — derived from :attr:`ToolDefinition.purity`, never a hardcoded
    name list (see the module-level note above this function)."""
    if tool_name in _MCP_DYNAMIC_DISPATCH_EXEMPT:
        return False
    definition = _default_tool_registry().lookup(tool_name)
    if definition is None:
        return False
    return definition.purity == "read_only"


def _gutter_glyph_color(msg: "OutboxMessage") -> "tuple[str, str]":
    """The gutter glyph + kind-colour for one display frame, keyed off ``_KIND_LINE``.

    Mirrors the plain renderer's marker column: the ``_KIND_LINE`` glyph (its
    leading non-space char) for message-y kinds, the ``●`` tool-header /  ``⎿``
    tool-result markers otherwise. The colour returned here is the KIND colour;
    a non-DEFAULT :class:`EntryState` overrides it in :meth:`ReynGutter.decorate`
    (state-driven colour, Phase 2). Kept cheap — ``decorate`` runs on every repaint.

    #3329: a SUCCESSFUL retrieval tool call (started/completed, never
    failed — a failure still needs the operator's attention regardless of
    the tool's op-class, so ``tool_call_failed`` is deliberately NOT
    demoted below) carries no gutter marker at all — see
    :func:`_is_retrieval_tool`.
    """
    kind = msg.kind
    if kind == "tool_call_started":
        if _is_retrieval_tool(str((msg.meta or {}).get("tool", ""))):
            return "", _CC_DIM
        return "●", _CC_TEXT
    if kind == "tool_call_completed":
        if _is_retrieval_tool(str((msg.meta or {}).get("tool", ""))):
            return "", _CC_DIM
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


def _cell_pad_right(label: str, width: int) -> str:
    """Left-align ``label`` in a ``width``-CELL column — the LEFT gutter's
    counterpart to :func:`_cell_pad_left` (the RIGHT gutter's helper, #3347).

    ``str.ljust`` pads by CHARACTER count; a gutter column is measured in
    terminal CELLS, and the two differ for any double-width glyph. Padding on
    :func:`rich.cells.cell_len` — the same measurement Textual's compositor
    applies to the resulting strip — keeps this cell correct by construction
    rather than by the coincidence that today's glyph vocabulary (``·  ⋯ ⎿ ◆
    ○ ● ✗ ❯``) happens to measure one cell each. Over-long labels are returned
    unpadded (flowview's own ``adjust_cell_length`` clips them; they never
    steal body columns)."""
    return label + " " * max(0, width - cell_len(label))


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
    non-animated amber gutter).

    ``is_streaming`` (#3530) reports whether an entry is an agent reply still
    RECEIVING chunks, and makes its marker blink with the same frames. The owner
    asked to be able to tell "waiting for the next chunk" from "the end
    arrived", and a streamed reply cannot answer that from its text alone —
    prose simply stops, whether because the model paused or because it finished.

    ★ The answer is READ, never inferred: the app's ``_streaming_replies`` map
    holds a record per in-flight ``chain_id`` and the TERMINAL COMPLETION FRAME
    pops it. So "still streaming" is the presence of that record, and no
    "nothing arrived for N seconds" heuristic is involved — a slow model and a
    finished one are distinguishable facts here, not a timing guess.

    Blinking (rather than a distinct glyph) is what makes the two states read as
    one: an entry that is streaming animates and the SAME marker goes still when
    the terminal frame lands, so the transition is the information. It reuses
    ``EntryState.RUNNING``'s frames deliberately — the pane already teaches
    "blinking marker == working" on tool rows — but keeps the entry's own kind
    COLOUR rather than RUNNING's amber, because amber is this gutter's
    needs-you/at-risk vocabulary and a reply arriving normally is neither.

    This gutter carries STATE only. The addressed-row rail lived here until
    #3526 moved it to the right gutter on the owner's instruction — see
    :class:`ReynRightGutter`, which now owns ``is_marked``."""

    def __init__(
        self,
        *,
        frame_period: float = _RUNNING_FRAME_PERIOD,
        clock: "Callable[[], float]" = time.monotonic,
        is_streaming: "Callable[[Entry[OutboxMessage]], bool] | None" = None,
    ) -> None:
        self._frame_period = frame_period
        self._clock = clock
        self._is_streaming = is_streaming

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
            # #3530: a reply still receiving chunks blinks in its OWN colour.
            # Checked after the RUNNING branch so a genuine EntryState is never
            # overridden by it, and only on DEFAULT rows, which is where a
            # streamed agent reply lives.
            if self._is_streaming is not None and self._is_streaming(entry):
                glyph = self._running_frame()
        else:
            color = _STATE_COLOR.get(state, kind_color)
        return Text(_cell_pad_right(glyph, width), style=color)


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


#: Rendered when the row NAMES a turn (its display frame carries a
#: ``chain_id``) but the runtime holds NO token figure for that turn — it
#: recorded no LLM call, or its bucket has been evicted from
#: ``BudgetTracker``'s bounded per-turn buckets (``TURN_BUCKET_CAP``), or this
#: is a remote client where the per-turn buckets are not on the wire at all.
#: Explicitly NOT ``"0"`` — a turn CAN legitimately record 0 tokens (a provider
#: that returned no usage), and that is a real figure this must stay distinct
#: from. Explicitly not an empty cell either, which on a row that names a turn
#: would read as "this turn used nothing".
TURN_USAGE_UNKNOWN = "—"

#: The display-frame kind a PER-CALL absolute token figure is anchored to —
#: the agent reply, i.e. the row that carries that specific litellm call's
#: own real ``prompt_tokens``/``completion_tokens`` in its meta. See
#: :class:`ReynTurnUsageGutter` for why this and :data:`TURN_TOTAL_ANCHOR_KIND`
#: are two DIFFERENT rows now, not one row wearing two meanings.
TURN_ANCHOR_KIND = "agent"

#: The display-frame kind the TURN TOTAL is anchored to — the ``"user"`` row
#: that opens the turn, never an ``agent`` row (#4691 arc item ④, owner
#: ruling). See :class:`ReynTurnUsageGutter`'s docstring for why an agent
#: row's own per-call figure and a turn's aggregate total were AMBIGUOUS
#: sharing one anchor: the same visual slot answered two different
#: questions depending on an incidental fact (did THIS specific frame happen
#: to carry its own ``prompt_tokens``?) invisible to the reader.
TURN_TOTAL_ANCHOR_KIND = "user"

#: Direction markers for the per-turn token split — ``↑`` what the turn SENT
#: (prompt) and ``↓`` what it generated (completion), e.g. ``"↑12k ↓1.8k"``.
#:
#: ★ Both are East Asian **Ambiguous** width (``unicodedata.east_asian_width``
#: → ``"A"``), so their column count is not universally fixed. MEASURED, not
#: assumed: ``rich.cells.cell_len`` resolves ambiguous to **1**, and that is
#: the SAME function Textual's compositor measures this strip with — so the
#: column arithmetic here and the renderer's agree by construction, and
#: :data:`RIGHT_GUTTER_WIDTH` derived via :func:`_cell_pad_left` cannot
#: overflow in process.
#:
#: They introduce no NEW class of risk: :data:`TURN_USAGE_UNKNOWN` (``—``,
#: U+2014) and the LEFT gutter's ``●``/``○``/``◆`` are ambiguous-width too and
#: have shipped since #3273. The residual — a terminal whose font/locale
#: renders ambiguous-width as 2 columns — is a property of the terminal, not
#: observable from inside this process, and applies equally to those existing
#: glyphs.
PROMPT_TOKENS_MARKER = "↑"
COMPLETION_TOKENS_MARKER = "↓"


def _cell_pad_left(label: str, width: int) -> str:
    """Right-align ``label`` in a ``width``-CELL column.

    ``str.rjust`` pads by CHARACTER count; a gutter column is measured in
    terminal CELLS, and the two differ for any double-width glyph. Padding on
    :func:`rich.cells.cell_len` — the same measurement Textual's compositor
    applies to the resulting strip — keeps this cell correct by construction
    rather than by the coincidence that today's vocabulary happens to be all
    one-cell. Over-long labels are returned unpadded (flowview's own
    ``adjust_cell_length`` clips them; they never steal body columns)."""
    return " " * max(0, width - cell_len(label)) + label


def _format_tokens(tokens: int) -> str:
    """A compact token count — ``"812"`` / ``"1.9k"`` / ``"120k"`` / ``"1.2M"``
    — bounded to 4 characters for any turn under a billion tokens, so
    :data:`RIGHT_GUTTER_WIDTH` can be computed rather than guessed.

    Each band's upper edge is the value that would ROUND into the next band
    (9_950 → ``"9.9k"``, not ``"10.0k"``), so the 4-character bound holds at the
    boundaries too rather than only in the middle of each band."""
    n = max(0, int(tokens))
    if n < 1_000:
        return str(n)
    if n < 9_950:
        return f"{n / 1_000:.1f}k"
    if n < 999_500:
        return f"{round(n / 1_000)}k"
    if n < 9_950_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{round(n / 1_000_000)}M"


#: FlowView RIGHT-gutter column width (Phase ④, #3283) — wired into ``app.py``'s
#: ``FlowView(right_gutter_width=…)`` config. COMPUTED in terminal CELLS
#: (:func:`rich.cells.cell_len`, the renderer's own measure) from the widest
#: label of each family this column renders, not guessed:
#:
#: - elapsed (:func:`_format_elapsed`) — 3 (``"99s"``);
#: - per-turn tokens (:class:`ReynTurnUsageGutter`) — the two-figure split
#:   ``↑<prompt> ↓<completion>``: marker (1) + :func:`_format_tokens` (4) +
#:   space (1) + marker (1) + :func:`_format_tokens` (4) = **11**
#:   (e.g. ``"↑120k ↓120k"``). Both markers measure 1 cell — see
#:   :data:`PROMPT_TOKENS_MARKER` for the ambiguous-width measurement.
#:
#: The two families are mutually exclusive on any one row by construction
#: (elapsed lives on ``tool_call_started`` rows, the token figure on
#: :data:`TURN_ANCHOR_KIND`/:data:`TURN_TOTAL_ANCHOR_KIND` rows), so the
#: widest cell is ``max(3, 11) = 11``,
#: not their sum — 12 + one column of breathing room = 13 (#4691 Phase A.5
#: raised this from 12 — the signed ↑ half costs one more character than the
#: absolute figure it replaced). If that exclusivity ever stopped holding,
#: the combined label would simply be clipped by flowview's own fixed-width
#: gutter — never allowed to steal body columns.
#:
#: ★ Bound re-justified against #3337's body-width floor gate at THIS width
#: (``test_right_gutter_leaves_the_body_at_least_half_the_terminal_width``):
#: on an 80-column terminal the body keeps ``80 - 2 (left gutter) - 12 = 66``
#: columns against a 40-column floor.
RIGHT_GUTTER_WIDTH = 12


class ReynTimingGutter:
    """Fills the flowview RIGHT gutter with a per-entry ELAPSED-TIME label
    (Phase ④, #3283) — the right-gutter half of the #3283 spec's "left gutter
    keeps state, right gutter shows per-entry metadata" split. The LEFT gutter
    (:class:`ReynGutter`) is untouched by this class.

    **This class contributes the ELAPSED half only.** The per-turn token half
    is :class:`ReynTurnUsageGutter`, and :class:`ReynRightGutter` is the
    decorator actually wired into ``FlowView(right_decorator=…)`` — it joins
    both labels into the one shared column. Of the umbrella issue's three
    right-gutter CANDIDATES, that leaves one still dropped:

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

    def label(self, entry: "Entry[OutboxMessage]") -> str:
        """This entry's elapsed-time label, or ``""`` when it has no timing
        data. The composable half of :meth:`decorate` — :class:`ReynRightGutter`
        joins this with :meth:`ReynTurnUsageGutter.label`."""
        meta = entry.item.meta or {}
        since = meta.get(_RUNNING_SINCE_KEY)
        if isinstance(since, (int, float)):
            return _format_elapsed(self._clock() - since)
        final = meta.get(_ELAPSED_SECS_KEY)
        return _format_elapsed(final) if isinstance(final, (int, float)) else ""

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        return Text(_cell_pad_left(self.label(entry), width), style=_CC_AMBIENT)


class ReynTurnUsageGutter:
    """Fills the flowview RIGHT gutter with a per-entry PER-TURN TOKEN COUNT
    (Phase ④ remainder, #3283) — the half #3337 had to leave out because the
    data did not exist yet.

    **The data now exists, and only because it was captured at the source.**
    #3339/#3342 keyed every LLM call's real tokens+cost by its turn's
    ``chain_id`` (``BudgetTracker``'s bounded per-turn buckets). This class
    reads those buckets through an injected ``usage_lookup`` —
    ``(chain_id) -> {"chain_id", "tokens", "cost_usd"} | None``, in production
    ``Session.turn_usage`` reached via the status snapshot's ``turn_usage_fn``.
    Nothing here derives a figure by differencing cumulative counters; that was
    rejected repeatedly across this arc and there is no code path for it.

    **Tokens only — the USD cost is deliberately NOT displayed** (owner call).
    Tokens alone answer the question this column exists for, and dropping the
    cost figure leaves the conversation body room: a real-TTY read at 80
    columns found a wider gutter left long tables and code visibly cramped. The
    lookup still RETURNS ``cost_usd`` and every caller may use it — this is a
    presentation decision made here, not a narrowing of the data. ``/cost`` and
    the status line remain the surfaces for spend.

    **Prompt and completion are shown separately**, not as one total:
    ``↑<prompt> ↓<completion>`` (:data:`PROMPT_TOKENS_MARKER` /
    :data:`COMPLETION_TOKENS_MARKER`) — what the turn SENT versus what it
    generated, which a single total cannot express and which move for entirely
    different reasons (a growing context vs a long reply). The split is carried
    all the way from ``TokenUsage`` at the call site through
    ``BudgetTracker``'s per-turn buckets; it is never re-derived here.

    **Two DIFFERENT anchors for two DIFFERENT figures (#4691 arc item ④,
    owner ruling) — not one row wearing two meanings.** Earlier revisions of
    this class anchored BOTH the turn total and a per-call absolute figure
    to the same :data:`TURN_ANCHOR_KIND` (``"agent"``) row, falling back to
    the turn total whenever a specific agent row happened not to carry its
    own ``prompt_tokens``. That was AMBIGUOUS: the same visual slot answered
    "this call's own tokens" or "the whole turn's total" depending on an
    incidental fact — did THIS specific frame happen to carry per-call
    figures in its meta? — invisible to the reader. A reader could not tell
    which question a number on an agent row was answering without already
    knowing that frame's own hidden shape.

    - :data:`TURN_ANCHOR_KIND` (``"agent"``) rows now show ONLY their own
      per-call absolute figure, straight from ``entry.item.meta`` — never a
      turn-total fallback. A row with no per-call figure (a restored/legacy
      frame, or an agent-kind emit site that never threaded one through)
      renders an EMPTY cell — no ambiguous number, no silent substitution.
    - :data:`TURN_TOTAL_ANCHOR_KIND` (``"user"``) rows — the line that OPENS
      a turn — show the turn TOTAL via ``usage_lookup(chain_id)``, never a
      per-call figure (a user row carries no LLM call of its own to report).
      Anchoring to the opening line rather than a settled reply also means
      the figure never repeats across a turn's multiple ``agent`` rows the
      way the shared-anchor design risked (a tool-turn's explanatory text,
      a spawn ack — see router_loop.py — one turn, one total, one row).

    **Three distinct renders on the ``user`` anchor, and they are distinct
    on purpose:**

    - the row's turn HAS a figure → its token split (``"↑12k ↓1.8k"``).
      Including a REAL zero (``"↑0 ↓0"``): a turn whose calls reported no usage
      genuinely used 0 tokens, and that is a fact, not an absence;
    - the row NAMES a turn (``meta["chain_id"]``) but the runtime holds no
      figure for it → :data:`TURN_USAGE_UNKNOWN` (``"—"``). Covers a turn
      that made no LLM call, a turn EVICTED from the bounded buckets
      (``TURN_BUCKET_CAP`` — an old row scrolled far back), an unknown
      chain_id, and a REMOTE client (the buckets are session-local and not on
      the AG-UI wire). Never ``"0"`` — that is the real-zero figure above, and
      collapsing the two would report an unmeasured turn as a measured free
      one;
    - the row names NO turn at all → an EMPTY cell. There is no turn to report
      on, so there is nothing unknown about it either. This is where every
      RESTORED row lands: ``restore.project_restored_frames`` re-projects a
      persisted ``ChatMessage`` and does not carry ``chain_id`` onto the frame,
      and the per-turn buckets are in-memory live-session state that a restart
      does not rehydrate — so a restored conversation shows no token figures
      rather than reconstructed ones. Same live-vs-restore posture #3337 landed
      for elapsed.

    ``usage_lookup=None`` (no read model / pre-session / plain fallback) makes
    every ``user`` row's lookup unknown, so a row that names a turn still
    renders ``—`` rather than silently nothing.

    **The ↑ half of an agent row's per-call figure is the call's real
    ABSOLUTE ``prompt_tokens`` — an owner ruling (#4691), not a signed
    delta.** #4698 tried a signed context-growth delta between consecutive
    calls first, reasoning that an absolute figure duplicates ctx tab's own
    number in a second place. The owner's final ruling reversed this: an
    absolute figure is PRIMITIVE (a reader can derive the delta by
    subtracting adjacent rows), while a delta is DERIVED and the absolute
    value can never be recovered from it. #4698's own "9 exception cases"
    for a session-shared delta baseline (cross-purpose/cross-session
    pollution, ``/model`` switches, rewind, fork, compaction) all evaporate
    with an absolute figure — only "no usage on the response" (``None``,
    never a fabricated ``0``) remains. "Showing the jump" between calls is
    Group/fold's own job (#4691 Phase B): the fold's summary row is what
    concentrates a turn down to its call boundaries, not this column."""

    def __init__(
        self,
        *,
        usage_lookup: "Callable[[str], dict | None] | None" = None,
    ) -> None:
        self._usage_lookup = usage_lookup

    def label(self, entry: "Entry[OutboxMessage]") -> str:
        """This entry's token label — an agent row's OWN per-call figure, a
        user row's TURN total (``"—"`` when it names a turn with no known
        figure), or ``""`` when the row is neither / names no turn.

        A lookup that raises is treated exactly like "no figure" — this runs on
        every gutter repaint and must never be able to kill a render."""
        kind = entry.item.kind
        if kind == TURN_ANCHOR_KIND:
            return self._per_call_label(entry)
        if kind == TURN_TOTAL_ANCHOR_KIND:
            return self._turn_total_label(entry)
        return ""

    def _per_call_label(self, entry: "Entry[OutboxMessage]") -> str:
        """An agent row's OWN call figure, straight off its meta — never a
        turn-total fallback (#4691 arc item ④). Only ``entry.item.meta`` is
        read here; :attr:`_usage_lookup` never runs for an agent row."""
        meta = entry.item.meta or {}
        # #4691: every LIVE kind="agent" emit site stamps
        # prompt_tokens/completion_tokens (see router_loop.py). Both halves
        # are ABSOLUTE (owner ruling — see the class docstring for why an
        # absolute figure replaced #4698's signed-delta attempt): ↑ this
        # call's own real prompt_tokens, ↓ its own completion_tokens. No
        # figure here (a restored/legacy frame, or a future agent-kind emit
        # site that never threaded one through) is an EMPTY cell — #4691
        # arc item ④ removed the turn-total fallback that used to sit here,
        # which is what made this row's own figure ambiguous with a turn
        # total sharing the same slot.
        call_prompt = meta.get("prompt_tokens")
        call_completion = meta.get("completion_tokens")
        if isinstance(call_prompt, (int, float)) and isinstance(
            call_completion, (int, float)
        ):
            return (
                f"{PROMPT_TOKENS_MARKER}{_format_tokens(int(call_prompt))} "
                f"{COMPLETION_TOKENS_MARKER}{_format_tokens(int(call_completion))}"
            )
        return ""

    def _turn_total_label(self, entry: "Entry[OutboxMessage]") -> str:
        """The TURN total for a ``user`` row (#4691 arc item ④) — the line
        that opens the turn, looked up by ``meta["chain_id"]``. See the
        class docstring's "Three distinct renders" for the figure /
        :data:`TURN_USAGE_UNKNOWN` / empty-cell trichotomy this implements."""
        meta = entry.item.meta or {}
        chain_id = meta.get("chain_id")
        if not isinstance(chain_id, str) or not chain_id:
            return ""
        usage: "dict | None" = None
        if self._usage_lookup is not None:
            try:
                usage = self._usage_lookup(chain_id)
            except Exception:
                usage = None
        if not usage:
            return TURN_USAGE_UNKNOWN
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if not isinstance(prompt, (int, float)) or not isinstance(
            completion, (int, float)
        ):
            return TURN_USAGE_UNKNOWN
        return (
            f"{PROMPT_TOKENS_MARKER}{_format_tokens(int(prompt))} "
            f"{COMPLETION_TOKENS_MARKER}{_format_tokens(int(completion))}"
        )

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        return Text(_cell_pad_left(self.label(entry), width), style=_CC_AMBIENT)


class ReynRightGutter:
    """The RIGHT-gutter decorator actually wired into
    ``FlowView(right_decorator=…)`` — one column, two label families.

    flowview takes a single right decorator, and Phase ④ has two things to say
    about a row: how long it ran (:class:`ReynTimingGutter`) and how many tokens
    its turn used (:class:`ReynTurnUsageGutter`). Rather than fold both into one class,
    each keeps its own ``label`` and this composes them, joining whatever is
    non-empty with a single space and right-aligning the result. In practice
    exactly one of the two ever speaks for a given row (elapsed on
    ``tool_call_started``, the turn figure on :data:`TURN_ANCHOR_KIND`), which
    is what lets :data:`RIGHT_GUTTER_WIDTH` be the widest single label rather
    than the sum.

    ``clock`` and ``usage_lookup`` are passed straight through to the two halves
    (see their docstrings); both are injectable so a test can drive the live
    elapsed value and the per-turn lookup deterministically with real
    collaborators.

    ``is_marked`` (#3490, moved here from :class:`ReynGutter` by #3526) reports
    whether an entry is the ADDRESSED one — the keyboard cursor's position,
    which is also where search puts it (#3493: one position, so never two marked
    rows). A marked entry gets :data:`_MARK_RAIL` drawn down its whole post-wrap
    height, which is why the mark lives in a GUTTER at all rather than in the
    ``flowview--highlight`` component style (``--selected`` was its 0.11.x
    synonym; #3624 / flowview 0.12.0 dropped the alias) — a reason unchanged
    by the move to this side: a component style is applied as a *base* under
    each segment's own attributes (``Segment.apply_style`` =
    ``style + segment.style``; flowview passes no ``post_style``), so a
    background there is swallowed on exactly the rows that carry a full-row
    ``Presentation.background`` — the user's own line and any failure row. A
    gutter is CONTENT, so it renders identically on every row kind. Defaults to
    "nothing is marked", leaving the right gutter byte-identical for every
    caller that does not pass it."""

    def __init__(
        self,
        *,
        clock: "Callable[[], float]" = time.monotonic,
        usage_lookup: "Callable[[str], dict | None] | None" = None,
        is_marked: "Callable[[Entry[OutboxMessage]], bool] | None" = None,
    ) -> None:
        self._timing = ReynTimingGutter(clock=clock)
        self._turn_usage = ReynTurnUsageGutter(usage_lookup=usage_lookup)
        self._is_marked = is_marked

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        parts = [
            label
            for label in (self._timing.label(entry), self._turn_usage.label(entry))
            if label
        ]
        label = " ".join(parts)
        if self._is_marked is None or not self._is_marked(entry):
            return Text(_cell_pad_left(label, width), style=_CC_AMBIENT)
        # The addressed entry: the rail in this gutter's LEADING cell, spanning
        # the body's full post-wrap ``height`` so a multi-row reply reads as ONE
        # marked block. The labels keep their own dim styling — being addressed
        # is a POSITION, so it must not repaint the metadata vocabulary beside
        # it, exactly as it did not repaint the state glyph on the left.
        rail = Text()
        for row in range(max(1, height)):
            if row:
                rail.append("\n")
            rail.append(_MARK_RAIL, style=_MARK_COLOR)
            rail.append(_cell_pad_left(label if row == 0 else "", width - 1), style=_CC_AMBIENT)
        return rail
