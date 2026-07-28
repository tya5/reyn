"""Body presentation for the Textual chat surface's flowview.

:class:`ReynPresenter` fills the flowview's body cell for one display frame,
reusing the plain renderer's palette + per-kind body construction (``_CC_*`` /
``_KIND_LINE`` / ``_body_renderable``) via :func:`_body_and_background` rather
than inventing a second styling vocabulary. The gutter column is the
:class:`~reyn.interfaces.inline.textual_chat.gutter.ReynGutter`'s job.

This module is part of the TTY-only ``textual_chat`` package (imported lazily
via :mod:`reyn.interfaces.repl.client_driver`); its ``textual_flowview`` import
never reaches an always-loaded module.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from rich.console import Console, Group, RenderableType
from rich.text import Text
from textual.content import Content
from textual_flowview import Presentation

from reyn.interfaces.repl.renderer import (
    _CC_ACCENT,
    _CC_DIM,
    _CC_DONE,
    _CC_ERR,
    _CC_ERR_BG,
    _CC_TEXT,
    _CC_USER_BG,
    _KIND_LINE,
    _SPINNER,
    _body_renderable,
    _summarize_args,
    summarize_tool_result,
)

from ._meta_keys import ORPHANED_RESULT_KIND as _ORPHANED_RESULT_KIND
from ._meta_keys import RESULT_KIND_KEY as _RESULT_KIND_KEY
from ._meta_keys import RESULT_META_KEY as _RESULT_META_KEY
from ._meta_keys import RUNNING_SINCE_KEY as _RUNNING_SINCE_KEY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reyn.runtime.outbox import OutboxMessage

# --- live RUNNING-tool indicator (Phase ②) ---------------------------------
# A ``tool_call_started`` entry that is in flight carries a monotonic START
# timestamp under ``_RUNNING_SINCE_KEY`` (stamped app-side by
# ``TextualChatApp._begin_running_indicator`` when the entry goes RUNNING — tool
# frames themselves carry no elapsed/progress, ADR finding D2). Its PRESENCE is
# what tells :meth:`ReynPresenter.present` to render the live spinner + elapsed
# body instead of the static ``tool(args)`` line; the completion handler REMOVES
# it to settle the row back to static. Defined in :mod:`._meta_keys` (imported
# above) because the right-gutter's live-elapsed decorator (Phase ④,
# :mod:`.gutter`) needs the SAME key — two producers/readers must agree on the
# exact string, same rationale as :data:`_RESULT_KIND_KEY` above.

# The braille-spinner advance rate (frames/sec), reusing the plain renderer's
# working-line idiom (``_SPINNER[int(now * 8) % len]`` — see
# :func:`reyn.interfaces.repl.status.working_line`) so the Textual tool spinner
# reads identically to the bottom-toolbar working spinner.
_SPINNER_SPEED = 8

# --- coalesced tool call + result (one entry, CC ``⏺ tool(args)`` + ``⎿ result``)
# When a tool completes, the frame pump SETTLES its RUNNING started entry IN PLACE
# — folding the result into the SAME entry rather than appending a separate result
# row (mirrors CC's block + the PoC's ``_present_tool_call`` grouping). The pump
# stashes the completion frame's kind + meta under these keys on the started
# item; their presence tells the presenter to render the ``⎿ <result>`` line
# under the ``tool(args)`` header. A completion with no matching started entry is
# still appended as its own row (kept via :func:`_body_and_background`'s
# ``tool_call_completed`` / ``tool_call_failed`` branches), so nothing regresses.
# The restore path (``restore.py``) stamps the SAME two keys onto its projected
# frames so a restored tool turn coalesces identically — both modules import
# the string values from ``_meta_keys`` (restore.py must stay textual-free, so
# the constants live there rather than here). A THIRD, orphan case (#72): when
# the turn ends with a tool still RUNNING (its completion never arrived), the
# app force-settles it at the turn boundary with ``_RESULT_KIND_KEY`` stamped
# to the sentinel :data:`_ORPHANED_RESULT_KIND` rather than a real completion
# kind — :func:`_tool_result_line` renders that as a NEUTRAL dim
# ``⎿ (no result — turn ended)`` line, never a ``✗`` failure.

# --- intervention pending / resolved flow entry -----------------------------
# #3299 P1: an intervention's INTERACTION (closed-set select / free-text
# answer) moved OUT of the FlowView into the grouped
# ``InterventionPanel`` widget between the flow and the input row — the
# FlowView is append-only history, and a mutable interactive control (the old
# in-flow clickable chips) fought that. The flow entry for an intervention is
# now a THIN pending placeholder (prompt head + a dim hint pointing at the
# panel) while pending, and a basic resolved "answered" record once the panel
# delivers an answer (the placeholder→resolved IN-PLACE churn-zero contract —
# same entry, ``set_item`` — is P2 polish; this is deliberately basic).
_PENDING_HINT = "  ⋯ respond in the panel below"


def option_content_rows(rows: "Sequence[str]") -> "list[Content]":
    """Wrap each row in a literal :class:`~textual.content.Content` — never a
    bare ``str`` handed to :class:`~textual.widgets.OptionList`.

    ``OptionList`` markup-parses a bare ``str`` option exactly like
    ``Static``/``RadioButton`` do (``Option.prompt`` → ``textual.visual.
    visualize(..., markup=True)`` by default, unset here) — the ``#3302``
    bracket-eating class, reached through a different widget. Any option row
    whose text is NOT an operator/config-derived identifier the operator
    themselves typed into ``reyn.yaml`` must go through here.

    Lives in this module (the display-boundary leaf that also owns
    :func:`_neutralized_label`) rather than in ``chrome`` because it now has
    two independent consumers — the History drawer pane
    (``chrome._history_option_content``) and the ``/``-``:`` completion popup
    (``completion.CompletionPopup``, whose ``/image`` candidates are FILESYSTEM
    names) — and ``chrome`` imports ``completion``, so the shared idiom cannot
    live there without a cycle. Re-deriving it per consumer is exactly how one
    call site ends up safe and the other broken (the #3302 fix's own history:
    a fresh History tab was safe on the build path and unsafe on the refresh
    path).
    """
    return [Content(row) for row in rows]


def _neutralized_label(label: str) -> str:
    """Neutralize an LLM-derived choice label at this display boundary.

    ``meta["choices"]`` labels reach here RAW (``session._iv_meta`` copies
    ``choice.label`` verbatim; only the ``nodes`` render-model path is
    neutralized at the source). Route each label through the SAME terminal
    neutralizer ``present``'s leaf seam uses (ESC/control strip, FP-0054) before
    it reaches the terminal, so a control/ESC sequence in a label can't drive
    the terminal.
    """
    from reyn.core.present.guard import get_neutralizer

    return get_neutralizer("terminal").neutralize(label)[0]


def _intervention_head(msg: "OutboxMessage") -> RenderableType:
    """The prompt head of an intervention entry (prompt + optional detail),
    shared by BOTH closed-set and free-text interventions.

    Built from the STRUCTURED ``meta`` fields (``prompt`` / ``detail``, added by
    ``session._iv_meta`` for exactly this "TUI renderers build a head without
    re-parsing the text" use) rather than the ``nodes`` render-model — the
    ``nodes`` list already embeds any choice labels as a bullet list, and the
    interactive form itself now lives in the :class:`InterventionPanel`
    (#3299 P1), so the flow entry never needs to re-render the options. Leaves
    are neutralized at this boundary (the structured fields are copied raw by
    ``_iv_meta``)."""
    meta = msg.meta or {}
    prompt = _neutralized_label(str(meta.get("prompt") or msg.text or " "))
    parts: "list[RenderableType]" = [Text(prompt, style=_CC_TEXT)]
    detail = meta.get("detail")
    if detail:
        parts.append(Text(_neutralized_label(str(detail)), style=_CC_DIM))
    return parts[0] if len(parts) == 1 else Group(*parts)


def _tool_head(msg: "OutboxMessage") -> Text:
    """The ``tool(args)`` header line of a tool-call row.

    The SINGLE source of that header, shared by the static
    :func:`_body_and_background` tool-call branch and the live
    :meth:`ReynPresenter._present_running_tool` indicator, so a RUNNING row and
    its settled form read identically (only the appended spinner/elapsed line
    differs while in flight)."""
    meta = msg.meta or {}
    tool = str(meta.get("tool", msg.text))
    args = _summarize_args(meta.get("args"))
    return Text.assemble((tool, "bold"), (f"({args})", _CC_DIM))


def _tool_result_line(msg: "OutboxMessage") -> "tuple[Text, str | None]":
    """The ``⎿ <result summary>`` sub-line + optional coral tint of a SETTLED tool
    row — the completion folded into its started entry (:data:`_RESULT_KIND_KEY`).

    Reuses the plain renderer's tool-result summary (:func:`summarize_tool_result`)
    and failure vocabulary so a coalesced result reads the same as the pre-coalesce
    separate ``tool_call_completed`` / ``tool_call_failed`` row — only the nesting
    (``⎿`` under the call, in one entry) is new. A failure tints the whole row
    with the dark failure block (:data:`_CC_ERR_BG`) and keeps the coral
    ``_CC_ERR`` text legible on top of it, matching the standalone failure row."""
    meta = msg.meta or {}
    result_meta = meta.get(_RESULT_META_KEY) or {}
    if meta.get(_RESULT_KIND_KEY) == _ORPHANED_RESULT_KIND:
        # Force-settled at the turn boundary (#72): the tool's report never
        # arrived. NEUTRAL, not a failure — no ``✗``, no coral tint (the #3296
        # don't-fabricate-a-failure lesson).
        return Text("  ⎿ (no result — turn ended)", style=_CC_DIM), None
    if meta.get(_RESULT_KIND_KEY) == "tool_call_failed":
        err = (
            result_meta.get("error_message")
            or result_meta.get("error_kind")
            or result_meta.get("text")
            or ""
        )
        return Text(f"  ⎿ ✗ {err}", style=_CC_ERR), _CC_ERR_BG
    summary = summarize_tool_result(meta.get("tool"), result_meta.get("result"))
    failed = summary.startswith("✗")
    return (
        Text(f"  ⎿ {summary}", style=_CC_ERR if failed else _CC_DIM),
        (_CC_ERR_BG if failed else None),
    )


def _running_indicator(msg: "OutboxMessage", now: float) -> Text:
    """The live ``⠙ elapsed Ns`` indicator line for an in-flight tool row.

    A time-driven braille spinner (:data:`_SPINNER`, advanced at
    :data:`_SPINNER_SPEED` off ``now``) plus the app-computed elapsed seconds
    since the entry went RUNNING (``now - meta[_RUNNING_SINCE_KEY]``). ``now`` is
    the caller's monotonic clock reading; because it is re-read on each
    ``animate_entry`` tick, both the spinner frame and the elapsed count advance
    with wall time (the live-indicator non-vacuity). Elapsed is clamped at 0 so a
    clock skew never renders a negative age."""
    meta = msg.meta or {}
    since = float(meta.get(_RUNNING_SINCE_KEY) or now)
    elapsed = max(0, int(now - since))
    frame = _SPINNER[int(now * _SPINNER_SPEED) % len(_SPINNER)]
    return Text.assemble((f"{frame} ", _CC_ACCENT), (f"elapsed {elapsed}s", _CC_DIM))


def _body_and_background(msg: "OutboxMessage") -> "tuple[RenderableType, str | None]":
    """The body renderable + optional full-row background for one display frame.

    Reuses the plain renderer's per-kind body construction (markdown for the
    agent reply, the tool-summary helpers for tool rows, the ``_KIND_LINE`` body
    style otherwise) so a frame reads the same here as in the plain scrollback.
    The user's own line carries its background via ``Presentation.background``
    (flowview paints it edge to edge across gutter + body), matching the plain
    renderer's faint user block without a hand-rolled grid. A FAILURE row
    (``tool_call_failed`` / ``error`` / a ``tool_call_completed`` whose summary
    is an ``✗`` failure) carries ``background=_CC_ERR_BG`` so the whole row is
    tinted with the dark failure block edge to edge — CC's block-tint of a failed
    tool (Phase 2).

    **Foreground and background are picked independently here, so they must
    never resolve to the same colour** (#3367). Every row tint is a ``_CC_*_BG``
    constant — a faint dark block — and every text/glyph colour is a ``_CC_*``
    foreground constant; the two vocabularies do not overlap, which is what makes
    the no-collision property hold by construction rather than per branch. Before
    #3367 the five failure legs (two here, two in :func:`_tool_result_line`, plus
    the ``error`` kind whose ``_KIND_LINE`` body style is already ``_CC_ERR``)
    each paired ``style=_CC_ERR`` with ``background=_CC_ERR``, painting the text
    in its own background colour. Because flowview paints the row background
    across the gutter column too, the left gutter's coral ``✗``/``⎿`` glyph
    (``ReynGutter``, ``_STATE_COLOR[EntryState.ERROR] == _CC_ERR``) vanished with
    it — one background choice, every foreground on the row. The gate is
    ``tests/test_textual_chat_row_contrast_3367.py``, which enumerates the
    (kind, state) pairings from the producers rather than a hand-written list.
    """
    kind = msg.kind
    meta = msg.meta or {}
    if kind == "presentation":
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta.get("nodes", [])), None
    # kind == "intervention" is intercepted earlier, in ``ReynPresenter.present``
    # (the pending/resolved placeholder — #3299 P1), so it never reaches here.
    if kind == "tool_call_started":
        head = _tool_head(msg)
        if meta.get(_RESULT_KIND_KEY) is not None:
            # Settled (coalesced): the tool call folded together with its result.
            result_line, background = _tool_result_line(msg)
            return Group(head, result_line), background
        return head, None
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        failed = summary.startswith("✗")
        style = _CC_ERR if failed else _CC_DIM
        return Text(summary, style=style), (_CC_ERR_BG if failed else None)
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        return Text(f"✗ {err}", style=_CC_ERR), _CC_ERR_BG
    line = _KIND_LINE.get(kind)
    body_style = line[2] if line else _CC_TEXT
    body = _body_renderable(kind, msg.text or " ", body_style)
    if kind == "user":
        background = _CC_USER_BG
    elif kind == "error":
        background = _CC_ERR_BG
    else:
        background = None
    return body, background


class ReynPresenter:
    """Turns a reyn display frame into a body :class:`Presentation` sized to
    ``width`` — reusing the plain renderer's palette + per-kind body construction
    (``_CC_*`` / ``_KIND_LINE`` / ``_body_renderable``), never a second styling
    vocabulary. The gutter is the :class:`ReynGutter`'s job.

    An in-flight tool-call row (``tool_call_started`` whose entry carries the
    :data:`_RUNNING_SINCE_KEY` meta marker) renders a LIVE body — the static
    ``tool(args)`` header plus a spinner + app-computed ``elapsed Ns`` line — at a
    FIXED height (header rows + 1) so the per-tick re-present never reflows. The
    body is re-derived on each ``animate_entry`` tick (viewport-gated), reading
    ``clock`` for the current spinner frame + elapsed; ``clock`` is injectable
    (default :func:`time.monotonic`) so a test drives the animation
    deterministically. The completion handler removes the marker, settling the row
    back to its static ``tool(args)`` form."""

    def __init__(self, *, clock: "Callable[[], float]" = time.monotonic) -> None:
        # A private probe console for measuring wrapped height at a given width.
        self._probe = Console()
        self._clock = clock

    def _measure(self, renderable: RenderableType, width: int) -> int:
        self._probe.size = (max(width, 1), 200)
        return max(
            len(
                self._probe.render_lines(
                    renderable, self._probe.options.update_width(max(width, 1))
                )
            ),
            1,
        )

    def _present_intervention_pending(
        self, item: "OutboxMessage", width: int
    ) -> Presentation:
        """Present an intervention as a THIN flow entry (#3299 P1) — the
        interactive form (closed-set select / free-text input) lives entirely
        in the :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
        widget below the flow (``TextualChatApp._present_intervention``
        populates + focuses it), so the flow entry itself never renders chips
        or an input.

        Pending (no ``_answer_label`` meta yet): the neutralized prompt head
        plus a dim ``⋯ respond in the panel below`` hint — the "◆ needs you"
        amber gutter (kind-driven) already marks the row. Resolved
        (``_answer_label`` set by the app once the panel delivers an answer,
        OR by the restore projection reading a persisted answer straight off
        history — #3299 P4): the head plus a green ``✓ answered: <label>``
        line (basic — the placeholder→resolved in-place churn-zero polish is
        P2, not built here). ``_answer_label`` is neutralized at THIS call
        site — the SAME leaf-neutralization discipline ``_intervention_head``
        already applies to ``prompt``/``detail`` (#2770) — because a matched
        CLOSED-SET choice's label is model-supplied / untrusted the same way
        the prompt is; a live choice answer arrives here ALREADY neutralized
        (``InterventionPanel`` neutralizes labels at tab-build time) so this
        is idempotent for it, but a RESTORED answer arrives RAW (persisted
        RAW by design — neutralize only at display boundaries, never at
        write time), making this the ONE real neutralization boundary for the
        restore path's answer text."""
        meta = item.meta or {}
        head = _intervention_head(item)
        head_h = self._measure(head, width)
        answer = meta.get("_answer_label")
        if answer is not None:
            resolved = Text.assemble(
                ("  ✓ answered: ", _CC_DIM),
                (_neutralized_label(str(answer)), f"bold {_CC_DONE}"),
            )
            return Presentation(height=head_h + 1, renderable=Group(head, resolved))
        hint = Text(_PENDING_HINT, style=_CC_DIM)
        return Presentation(height=head_h + 1, renderable=Group(head, hint))

    def _present_running_tool(
        self, item: "OutboxMessage", width: int
    ) -> Presentation:
        """Present an in-flight tool-call row with a LIVE spinner + elapsed body.

        The static ``tool(args)`` header (:func:`_tool_head`) with a
        ``⠙ elapsed Ns`` line under it (:func:`_running_indicator`, read off
        ``self._clock``). Height is FIXED at ``header_rows + 1``: the header does
        not change while in flight and the indicator is always one line, so the
        per-tick re-present (driven by ``animate_entry``) never reflows — only the
        spinner frame + elapsed count advance. The completion handler strips the
        :data:`_RUNNING_SINCE_KEY` marker, after which :meth:`present` renders the
        static tool-call body instead (settle)."""
        head = _tool_head(item)
        indicator = _running_indicator(item, self._clock())
        head_h = self._measure(head, width)
        return Presentation(height=head_h + 1, renderable=Group(head, indicator))

    async def present(self, item: "OutboxMessage", width: int) -> Presentation:
        meta = item.meta or {}
        if item.kind == "intervention":
            return self._present_intervention_pending(item, width)
        if item.kind == "tool_call_started" and meta.get(_RUNNING_SINCE_KEY) is not None:
            return self._present_running_tool(item, width)
        body, background = _body_and_background(item)
        return Presentation(
            height=self._measure(body, width),
            renderable=body,
            background=background,
        )
