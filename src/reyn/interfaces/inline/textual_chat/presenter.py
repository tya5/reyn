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
from textual_flowview import Presentation

from reyn.interfaces.repl.renderer import (
    _CC_ACCENT,
    _CC_DIM,
    _CC_DONE,
    _CC_ERR,
    _CC_TEXT,
    _CC_USER_BG,
    _CC_WARN,
    _KIND_LINE,
    _SPINNER,
    _body_renderable,
    _summarize_args,
    summarize_tool_result,
)

if TYPE_CHECKING:
    from reyn.runtime.outbox import OutboxMessage

# --- live RUNNING-tool indicator (Phase ②) ---------------------------------
# A ``tool_call_started`` entry that is in flight carries a monotonic START
# timestamp under this meta key (stamped app-side by
# ``TextualChatApp._begin_running_indicator`` when the entry goes RUNNING — tool frames
# themselves carry no elapsed/progress, ADR finding D2). Its PRESENCE is what
# tells :meth:`ReynPresenter.present` to render the live spinner + elapsed body
# instead of the static ``tool(args)`` line; the completion handler REMOVES it to
# settle the row back to static. Kept private (leading underscore) so it never
# collides with a real display-frame meta field.
_RUNNING_SINCE_KEY = "_running_since"

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
_RESULT_KIND_KEY = "_result_kind"
_RESULT_META_KEY = "_result"

# --- choice-intervention chip layout ---------------------------------------
# A closed-set intervention (permission confirm / choice ``ask_user`` — anything
# carrying ``meta["choices"]``) is surfaced IN FLOW as clickable option chips on
# a single row below the prompt, mirroring the PoC's ``ask_user`` affordance and
# the old inline region's cursor+Enter picker. Selecting a chip resolves the
# intervention through ``transport.answer_intervention_choice`` (see
# ``TextualChatApp.on_flow_view_clicked``); the chip geometry here is the SINGLE
# source of truth both the presenter (draw) and the app (hit-test) read, so a
# click always maps to the chip that was drawn.
_CHOICE_INDENT = 2  # leading pad before the first chip (cols)
_CHOICE_GAP = 2  # gap between chips (cols)
_CHOICE_HINT = "  ↑ click an option"


def _choice_chip(index: int, label: str) -> str:
    """The rendered text of one option chip (``[ 1 · Yes ]``)."""
    return f"[ {index + 1} · {label} ]"


def choice_chip_spans(
    choices: "list[dict]",
) -> "list[tuple[int, int, str]]":
    """``(start_col, end_col, choice_id)`` per chip on the chip row.

    Shared by the presenter (which DRAWS the chips) and the app click handler
    (which HIT-TESTS a click's column against these spans), so the two never
    disagree about where a chip is. ``choice_id`` is the authoritative match key
    delivered to ``answer_intervention_choice`` — never displayed, so it is not
    neutralized here (only the visible label is, in :func:`_neutralized_label`).
    """
    spans: "list[tuple[int, int, str]]" = []
    col = _CHOICE_INDENT
    for i, choice in enumerate(choices):
        text = _choice_chip(i, _neutralized_label(choice.get("label", "")))
        spans.append((col, col + len(text), str(choice.get("id", ""))))
        col += len(text) + _CHOICE_GAP
    return spans


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


def _choice_head(msg: "OutboxMessage") -> RenderableType:
    """The prompt head of a choice-intervention entry (prompt + optional detail).

    Built from the STRUCTURED ``meta`` fields (``prompt`` / ``detail``, added by
    ``session._iv_meta`` for exactly this "TUI renderers build chips without
    re-parsing the text" use) rather than the ``nodes`` render-model, because the
    ``nodes`` list already embeds the choice labels as a bullet list — rendering
    it would DOUBLE the options (once as a list, once as chips). Leaves are
    neutralized at this boundary (the structured fields are copied raw by
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
    coral (``_CC_ERR``), matching the standalone failure row."""
    meta = msg.meta or {}
    result_meta = meta.get(_RESULT_META_KEY) or {}
    if meta.get(_RESULT_KIND_KEY) == "tool_call_failed":
        err = (
            result_meta.get("error_message")
            or result_meta.get("error_kind")
            or result_meta.get("text")
            or ""
        )
        return Text(f"  ⎿ ✗ {err}", style=_CC_ERR), _CC_ERR
    summary = summarize_tool_result(meta.get("tool"), result_meta.get("result"))
    failed = summary.startswith("✗")
    return (
        Text(f"  ⎿ {summary}", style=_CC_ERR if failed else _CC_DIM),
        (_CC_ERR if failed else None),
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
    is an ``✗`` failure) carries ``background=_CC_ERR`` so the whole row is
    tinted coral edge to edge — CC's block-tint of a failed tool (Phase 2).
    """
    kind = msg.kind
    meta = msg.meta or {}
    if kind == "presentation":
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta.get("nodes", [])), None
    if kind == "intervention" and meta.get("nodes") is not None:
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta["nodes"]), None
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
        return Text(summary, style=style), (_CC_ERR if failed else None)
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        return Text(f"✗ {err}", style=_CC_ERR), _CC_ERR
    line = _KIND_LINE.get(kind)
    body_style = line[2] if line else _CC_TEXT
    body = _body_renderable(kind, msg.text or " ", body_style)
    if kind == "user":
        background = _CC_USER_BG
    elif kind == "error":
        background = _CC_ERR
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

    def choice_chip_row(self, item: "OutboxMessage", width: int) -> int:
        """The body row (0-based) the option chips are drawn on at ``width``.

        The prompt head may wrap to several rows, so the chip row is not fixed —
        the app click handler calls this (with the SAME ``width`` the presenter
        drew at, ``FlowView._body_width()``) to know which ``event.y`` is the chip
        row before hit-testing the column against :func:`choice_chip_spans`.
        Recomputed (not stashed) so no client-side state is threaded through the
        immutable frame."""
        return self._measure(_choice_head(item), width)

    def _present_intervention_choice(
        self, item: "OutboxMessage", width: int
    ) -> Presentation:
        """Present a closed-set intervention as clickable option chips.

        Pending: the neutralized prompt head, an amber (``_CC_WARN``) chip row of
        ``[ n · label ]`` options, and a click hint — the "◆ needs you" amber
        gutter (kind-driven) already marks the row. Resolved (``_chosen_label``
        set by the click handler): the head plus a green ``✓ resolved: <label>``
        line, matching the PoC's resolved state (the gutter also goes green via
        ``EntryState.SUCCESS``)."""
        meta = item.meta or {}
        head = _choice_head(item)
        head_h = self._measure(head, width)
        chosen = meta.get("_chosen_label")
        if chosen is not None:
            resolved = Text.assemble(
                ("  ✓ resolved: ", _CC_DIM), (str(chosen), f"bold {_CC_DONE}")
            )
            return Presentation(height=head_h + 1, renderable=Group(head, resolved))
        chips = Text(" " * _CHOICE_INDENT)
        for i, choice in enumerate(meta.get("choices", [])):
            chips.append(
                _choice_chip(i, _neutralized_label(choice.get("label", ""))),
                style=f"bold {_CC_WARN}",
            )
            chips.append(" " * _CHOICE_GAP)
        hint = Text(_CHOICE_HINT, style=_CC_DIM)
        return Presentation(height=head_h + 2, renderable=Group(head, chips, hint))

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
        if item.kind == "intervention" and meta.get("choices"):
            return self._present_intervention_choice(item, width)
        if item.kind == "tool_call_started" and meta.get(_RUNNING_SINCE_KEY) is not None:
            return self._present_running_tool(item, width)
        body, background = _body_and_background(item)
        return Presentation(
            height=self._measure(body, width),
            renderable=body,
            background=background,
        )
