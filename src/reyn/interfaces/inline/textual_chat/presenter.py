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

import json
import time
from typing import TYPE_CHECKING, Callable

from rich.console import Console, Group, RenderableType
from rich.text import Text
from textual.content import Content
from textual_flowview import Entry, Presentation

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

from ._meta_keys import EXPANDED_KEY as _EXPANDED_KEY
from ._meta_keys import ORPHANED_RESULT_KIND as _ORPHANED_RESULT_KIND
from ._meta_keys import PIPELINE_RUN_KEY as _PIPELINE_RUN_KEY
from ._meta_keys import RESULT_KIND_KEY as _RESULT_KIND_KEY
from ._meta_keys import RESULT_META_KEY as _RESULT_META_KEY
from ._meta_keys import RUNNING_SINCE_KEY as _RUNNING_SINCE_KEY
from .gutter import _is_retrieval_tool

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
    two independent consumers — the drawer panes that need literal rows
    (``chrome._literal_option_content``) and the ``/``-``:`` completion popup
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


#: Width of a pipeline row's progress bar, in cells. Small on purpose: the row
#: shares a line with the pipeline's name and step, and a bar that dominates it
#: would say less than the numbers beside it already do.
_PIPELINE_BAR_CELLS = 12


def _pipeline_row(meta: dict) -> "RenderableType":
    """One pipeline RUN's row: name, a bar, and where the run is.

    The bar is ``rich.progress_bar.ProgressBar`` — Rich's own, not a hand-rolled
    ``"\u2501" * n``. A reimplementation looks equivalent at a glance and is
    not: the real one renders a HALF-cell tip (``\u2578``), so the bar advances
    at twice the resolution its width suggests — most of what makes a 12-cell
    bar worth drawing at all.

    Built from the numbers in ``meta`` rather than from the frame's text. The
    forwarder composes a sentence too, and parsing that back would couple this
    to its wording — the numbers are what the row is actually about.

    A run whose ``total_steps`` is unknown gets the step count with no bar,
    rather than a bar over a guessed denominator: a bar that is not measuring
    anything is worse than none.
    """
    from rich.progress_bar import ProgressBar
    from rich.table import Table

    name = str(meta.get("pipeline_name") or "pipeline")
    kind = str(meta.get("step_kind") or "?")
    index = meta.get("step_index")
    total = meta.get("total_steps")
    done = (index or 0) + (
        1 if meta.get("step_event") == "pipeline_step_completed" else 0
    )

    if not (isinstance(total, int) and total > 0):
        return Text.assemble((name, "bold"), (f"  step {done}  ", ""), (kind, _CC_DIM))

    row = Table.grid(padding=(0, 1))
    for _ in range(4):
        row.add_column()
    row.add_row(
        Text(name, style="bold"),
        # Styles defer to the terminal rather than taking Rich's default
        # magenta/green: a themed default is the user's to choose, and the bar
        # is not one of the two places this CUI claims a colour of its own.
        ProgressBar(
            total=total,
            completed=done,
            width=_PIPELINE_BAR_CELLS,
            style=_CC_DIM,
            complete_style="",
            finished_style="",
        ),
        Text(f"{done}/{total}"),
        Text(kind, style=_CC_DIM),
    )
    return row


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


#: How many lines of a full tool result the expanded view shows before it stops.
#: A cap, not a preference: the expansion is driven by the highlight simply
#: ARRIVING on a row, so an uncapped body would let a 10k-line result shove the
#: whole conversation off-screen as a side effect of pressing ``k`` — the reader
#: never asked for that. Anything beyond is summarised by the trailer line, and
#: the untruncated text is still reachable through the row's own copy
#: (``Enter``/``Space``), which copies ``item.text``, not what is on screen.
_EXPANDED_MAX_LINES = 40


def _dict_detail_lines(result: dict) -> "list[str]":
    """#4756: ``json.dumps(result, indent=2)``'s ``indent`` only inserts real
    newlines BETWEEN a dict's structural elements (keys/values) — never
    inside a string VALUE's own content, so a multi-line string field
    (``exec``'s ``stdout``, ``read_file``'s ``content``, ...) collapses to
    one JSON-escaped line of literal ``\\n`` text, defeating the whole
    point of expanding the row: the field a reader opened it to actually
    read becomes unreadable. Scoped to the TOP-LEVEL fields of a dict
    result (every #4756 repro — ``sandboxed_exec``, ``read_file`` — is
    this shape; a value nested a level deeper falls back to plain
    ``json.dumps`` for that sub-structure, unchanged from before this fix,
    since no repro exercises that depth).

    A top-level string field containing ``\\n`` gets its OWN real lines
    (opening ``"key": "``, its raw content lines verbatim, a closing
    ``"``), matching a human's mental model of "the file/output's actual
    lines" rather than JSON's escaped single-line encoding. Every other
    field keeps the ordinary compact ``"key": value,`` JSON rendering.

    lead-coder review, #4757: ``json.dumps`` incidentally neutralized
    terminal-control bytes in the value it was replacing (it escapes
    ``\\x1b`` etc. to ``\\u001b``) — an accidental side effect, not a
    designed defense, but a real one this fix silently removed by
    ``splitlines()``-ing the raw value verbatim. ``exec``'s ``stdout`` /
    ``read_file``'s ``content`` are arbitrary bytes from the world, not
    operator-typed ``reyn.yaml`` text — the SAME rule
    :func:`_neutralized_label` (this module) already states for exactly
    this reason (FP-0054): "text is not from the operator... must go
    through here". The multi-line value is neutralized via the SAME
    ``get_neutralizer("terminal")`` seam BEFORE splitting — its own
    control-char regex explicitly excludes tab/newline/carriage-return,
    so splitting on the neutralized value's real newlines is unaffected."""
    if not result:
        return ["{}"]
    from reyn.core.present.guard import get_neutralizer
    _terminal = get_neutralizer("terminal")
    lines: "list[str]" = ["{"]
    items = list(result.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        try:
            key_json = json.dumps(str(key), ensure_ascii=False)
        except Exception:
            key_json = f'"{key}"'
        if isinstance(value, str) and "\n" in value:
            clean_value, _ = _terminal.neutralize(value)
            lines.append(f"  {key_json}: \"")
            lines.extend(clean_value.splitlines())
            lines.append(f"\"{comma}")
        else:
            try:
                value_json = json.dumps(value, ensure_ascii=False, indent=2)
            except Exception:
                value_json = json.dumps(str(value), ensure_ascii=False)
            # Re-indent a multi-line nested structure (list/dict) by 2 spaces
            # so it still reads as nested under this key, not flush-left.
            value_lines = value_json.splitlines()
            if len(value_lines) == 1:
                lines.append(f"  {key_json}: {value_lines[0]}{comma}")
            else:
                lines.append(f"  {key_json}: {value_lines[0]}")
                lines.extend(f"  {line}" for line in value_lines[1:-1])
                lines.append(f"  {value_lines[-1]}{comma}")
    lines.append("}")
    return lines


def _result_detail_lines(msg: "OutboxMessage") -> "list[str]":
    """The FULL tool result as display lines — what the one-line summary drops.

    ``summarize_tool_result`` reduces a result to e.g. ``Read 42 lines``; the raw
    value survives on the frame (``meta[_RESULT_META_KEY]["result"]``), so the
    expansion is a different RENDERING of data the row already carries, not a
    re-fetch. A dict/list is pretty-printed rather than ``repr``'d so a JSON-ish
    result reads as structure; anything unprintable degrades to ``str`` rather
    than raising, because a presenter that raises takes the whole row down.

    #4756: a dict result routes through :func:`_dict_detail_lines` instead of
    a flat ``json.dumps`` — see that function's own docstring for why. A bare
    STRING result is neutralized here too (lead-coder review, #4757 follow-up
    sweep: this branch never went through ``json.dumps`` at all, so it was
    open to the SAME unneutralized-control-byte gap independently of, and
    predating, this PR's own dict fix — same function, same seam, closed in
    the same PR rather than left open one branch over)."""
    from reyn.core.present.guard import get_neutralizer
    result = ((msg.meta or {}).get(_RESULT_META_KEY) or {}).get("result")
    if result is None or result == "":
        return []
    if isinstance(result, str):
        text = get_neutralizer("terminal").neutralize(result)[0]
        return text.splitlines() or [text]
    if isinstance(result, dict):
        try:
            return _dict_detail_lines(result)
        except Exception:
            pass  # fall through to the generic json.dumps below
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        text = str(result)
    return text.splitlines() or [text]


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
        # #4762: err is WORLD-derived -- dispatcher.py's own
        # `message=f"{type(e).__name__}: {e}"` wraps ANY tool-handler
        # exception (an MCP call, a sandboxed subprocess, a provider HTTP
        # error), the same class #4758 fixed for tool_call_completed's own
        # stderr branch. #4758's own fix never covered this branch
        # (explicitly scoped out, tracked as #4762 -- measured here: err
        # does mix in external content, so this IS the same hole).
        return Text(f"  ⎿ ✗ {_neutralized_label(err)}", style=_CC_ERR), _CC_ERR_BG
    summary = summarize_tool_result(meta.get("tool"), result_meta.get("result"))
    failed = summary.startswith("✗")
    if meta.get(_EXPANDED_KEY) and not failed:
        # #3508: the row is expanded (Space toggles this, #4697/#4691§6 —
        # decoupled from highlight movement) — show the result the summary
        # dropped. The summary line is KEPT as the first line so the row reads
        # the same whether or not it is expanded; the detail is added under it
        # rather than replacing it, which is what makes toggling a row feel
        # like it's unfolding rather than swapping.
        lines = _result_detail_lines(msg)
        # Only unfold when the summary is actually WITHHOLDING something. For a
        # short scalar result ``summarize_tool_result`` falls back to the value
        # itself, so the "detail" is the summary again — expanding then printed
        # the same sentence twice (seen in a real terminal; the headless tests
        # used list results, which always elide, so they could not catch it).
        if len(lines) == 1 and lines[0].strip() in summary:
            lines = []
        if lines:
            body = Text(f"  ⎿ {summary}", style=_CC_DIM)
            for line in lines[:_EXPANDED_MAX_LINES]:
                body.append("\n")
                body.append(f"     {line}", style=_CC_DIM)
            if len(lines) > _EXPANDED_MAX_LINES:
                body.append("\n")
                body.append(
                    f"     … {len(lines) - _EXPANDED_MAX_LINES} more lines"
                    " · enter to copy the whole result",
                    style=_CC_DIM,
                )
            return body, None
    return (
        Text(f"  ⎿ {summary}", style=_CC_ERR if failed else _CC_DIM),
        (_CC_ERR_BG if failed else None),
    )


def _collapsed_retrieval_line(msg: "OutboxMessage") -> "Text | None":
    """#4662 (#3329's deferred body half): a SETTLED, non-expanded,
    non-failed retrieval tool call's row, folded to ONE dim line —
    ``tool(args) → summary`` — instead of the usual two-line ``tool(args)``
    header + ``⎿ summary`` result. Returns ``None`` when the row does not
    qualify (not settled, not retrieval, currently expanded, or failed),
    so the caller falls through to the ordinary two-line form unchanged.

    Reuses :func:`_is_retrieval_tool` (#3329's own ``ToolDefinition.purity``
    derivation — no second taxonomy) and the SAME
    :func:`summarize_tool_result` the two-line form already calls, so a
    row reads identically whichever shape it takes, just spread over one
    line instead of two.

    The full result is not lost: #3508's existing expand mechanism
    (:func:`_tool_result_line`, Space-toggled per #4697/#4691§6) still
    fires when the row is expanded, exactly as it does for a side-effect
    tool's row — this function only changes the COLLAPSED, non-expanded
    default, never removes the expand path.

    Failure is excluded on purpose, mirroring #3329's gutter demotion:
    a failed retrieval call still needs the operator's attention
    regardless of the tool's op-class, so it keeps the ordinary
    two-line/coral-tinted form."""
    meta = msg.meta or {}
    if meta.get(_EXPANDED_KEY):
        return None
    result_kind = meta.get(_RESULT_KIND_KEY)
    if result_kind is None or result_kind == "tool_call_failed":
        return None
    tool = str(meta.get("tool", msg.text))
    if not _is_retrieval_tool(tool):
        return None
    result_meta = meta.get(_RESULT_META_KEY) or {}
    if result_kind == _ORPHANED_RESULT_KIND:
        summary = "(no result — turn ended)"
    else:
        summary = summarize_tool_result(tool, result_meta.get("result"))
        if summary.startswith("✗"):
            return None  # a summary-shaped failure (D-2 kind, non-raising) — same exclusion
    args = _summarize_args(meta.get("args"))
    return Text.assemble(
        (tool, _CC_DIM), (f"({args})", _CC_DIM), (" → ", _CC_DIM), (summary, _CC_DIM),
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


def _body_and_background(
    msg: "OutboxMessage",
    *,
    neutralize_body: bool = False,
    image_cache: "dict[str, object] | None" = None,
    decoded_image_cache: "dict[str, object] | None" = None,
    now: float | None = None,
) -> "tuple[RenderableType, str | None]":
    """The body renderable + optional full-row background for one display frame.

    ``neutralize_body`` (#3318, default off) is forwarded to the shared
    ``_body_renderable`` call below — see its docstring.

    ``image_cache`` (#3846, default None) is forwarded to the ``presentation``
    kind's ``render_presentation_nodes`` call — see that function's own
    docstring for why it must stay a pure dict lookup. ``decoded_image_cache``
    (#4464, default None) is forwarded alongside it — see
    :func:`~reyn.interfaces.repl.present_renderer.render_presentation_nodes`'s
    own docstring for what it skips.

    ``now`` (#4464, default None) — when the entry carries
    :data:`_RUNNING_SINCE_KEY` (stamped by :meth:`TextualChatApp.
    _begin_running_indicator`, called for a ``presentation`` entry that still
    has an image resolving — see ``_begin_image_resolutions``), an extra
    :func:`_running_indicator` line is appended below the rendered nodes,
    reusing the EXACT SAME live-indicator convention the RUNNING tool-call
    row already uses (no new visual vocabulary — the owner's explicit
    "受入条件" for #4464 bans inventing one). A blank/unresolved image node
    otherwise renders its ordinary ``[image: alt]`` placeholder — this line
    is the only ADDITIONAL signal that something is actively in flight
    rather than merely not-yet-requested.

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
    ``tests/interfaces/test_textual_chat_row_contrast_3367.py``, which enumerates the
    (kind, state) pairings from the producers rather than a hand-written list.
    """
    kind = msg.kind
    meta = msg.meta or {}
    if kind == "presentation":
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        body = render_presentation_nodes(
            meta.get("nodes", []),
            image_cache=image_cache,
            decoded_image_cache=decoded_image_cache,
        )
        if meta.get(_RUNNING_SINCE_KEY) is not None and now is not None:
            body = Group(body, _running_indicator(msg, now))
        return body, None
    if meta.get(_PIPELINE_RUN_KEY) is not None:
        return _pipeline_row(meta), None
    # kind == "intervention" is intercepted earlier, in ``ReynPresenter.present``
    # (the pending/resolved placeholder — #3299 P1), so it never reaches here.
    if kind == "tool_call_started":
        if meta.get(_RESULT_KIND_KEY) is not None:
            # #4662: a settled, non-expanded, non-failed retrieval call folds
            # to ONE dim line instead of the usual header+result pair —
            # checked BEFORE the two-line form below, never after (the two-
            # line form is the fallback, not the default this overrides).
            collapsed = _collapsed_retrieval_line(msg)
            if collapsed is not None:
                return collapsed, None
            # Settled (coalesced): the tool call folded together with its result.
            head = _tool_head(msg)
            result_line, background = _tool_result_line(msg)
            return Group(head, result_line), background
        return _tool_head(msg), None
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        failed = summary.startswith("✗")
        style = _CC_ERR if failed else _CC_DIM
        return Text(summary, style=style), (_CC_ERR_BG if failed else None)
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text or ""
        # #4762: err is WORLD-derived -- see _tool_result_line's own #4762
        # comment above for the full trace (dispatcher.py's exception-
        # wrapping f-string). Same fix, this branch's own copy of it (the
        # pre-coalesce standalone tool_call_failed row, not the coalesced
        # nested one).
        return Text(f"✗ {_neutralized_label(str(err))}", style=_CC_ERR), _CC_ERR_BG
    line = _KIND_LINE.get(kind)
    body_style = line[2] if line else _CC_TEXT
    body = _body_renderable(
        kind, msg.text or " ", body_style, neutralize_body=neutralize_body
    )
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

    def __init__(
        self,
        *,
        clock: "Callable[[], float]" = time.monotonic,
        neutralize_body: bool = False,
    ) -> None:
        # A private probe console for measuring wrapped height at a given width.
        self._probe = Console()
        self._clock = clock
        # #3318: opt-in body ESC/OSC neutralize (chat.neutralize_body), default
        # off — see _body_and_background/_body_renderable's own docstrings.
        self._neutralize_body = neutralize_body
        # #3846 ②: settled image `src` resolutions, keyed by src — see
        # begin_image_resolution's own docstring. A plain dict (present() is
        # only ever awaited from the app's own single-threaded event loop, so
        # no lock is needed for this read/write pattern).
        #
        # #4376: reyn's chat FlowView model never sheds an individual entry
        # mid-session (only a full ``conversation.clear()`` on session
        # switch) — so there is no "left the model" signal to bind this
        # cache's lifetime to, the way flowview's own v0.17.0 memory-control
        # guide recommends for ITS OWN caches. Falling back to a total-byte
        # cap instead (see :meth:`_store_image_resolution`): without one,
        # every unique image `src` a session has ever resolved stays cached
        # for the session's full lifetime — #3876/#3872's class of bug
        # (unbounded accumulation, no eviction path), this time via image
        # bodies (up to 5MB each, ``image_fetch.DEFAULT_MAX_BYTES``) instead
        # of a list().
        self._image_cache: "dict[str, object]" = {}
        self._image_inflight: "set[str]" = set()
        # #4464: the DECODED renderable (a `present_renderer.HalfBlockImage`,
        # #4474), keyed by src — populated by `_resolve_image` on a
        # background thread once the fetch settles, so the CPU-heavy PIL
        # decode + resize (measured directly: ~100-300ms for a large real
        # photo) never runs on the event loop. Evicted in
        # lockstep with `_image_cache` (see `_store_image_resolution`) —
        # a separate cap of its own would drift from the byte cap that
        # already governs image lifetime here.
        self._decoded_image_cache: "dict[str, object]" = {}
        from reyn.core.present.image_fetch import DEFAULT_MAX_BYTES

        # Derived from DEFAULT_MAX_BYTES rather than a fresh magic number —
        # lead-coder's explicit requirement (#4376): a per-entry cap and a
        # total cap that are independently-chosen numbers drift out of sync
        # the moment only one of them is retuned. 10x is a starting bound
        # (matches DEFAULT_MAX_BYTES's own "a starting number, not a
        # measured one" spirit) — room for a normal chat's worth of images
        # without holding every image body a long session has ever shown.
        self._image_cache_byte_cap = DEFAULT_MAX_BYTES * 10
        self._image_cache_bytes = 0

    def _store_image_resolution(self, src: str, resolution: object) -> None:
        """The ONE mutation point for :attr:`_image_cache` writes (#4376),
        wrapping the dict assignment every call site used to do directly.
        Enforces :attr:`_image_cache_byte_cap` by evicting the OLDEST
        entries (FIFO — dict preserves insertion order, and `del` + re-
        assign moves an updated key to the end) until the new total fits.

        A single very-large entry (already itself over the cap — the file
        size cap is per-entry, so this cannot happen with a well-behaved
        fetch, but a future change to either constant should not corrupt
        state) is kept rather than evicted-then-immediately-re-evicted:
        with everything else already gone, evicting it too would leave the
        src the caller JUST resolved unexpectedly absent from its own
        cache.
        """
        body = getattr(resolution, "body", b"")
        size = len(body) if isinstance(body, (bytes, bytearray)) else 0
        if src in self._image_cache:
            old_body = getattr(self._image_cache[src], "body", b"")
            self._image_cache_bytes -= (
                len(old_body) if isinstance(old_body, (bytes, bytearray)) else 0
            )
            del self._image_cache[src]  # re-inserted below, now the newest
        self._image_cache[src] = resolution
        self._image_cache_bytes += size
        while self._image_cache_bytes > self._image_cache_byte_cap and len(self._image_cache) > 1:
            oldest_src = next(iter(self._image_cache))
            evicted = self._image_cache.pop(oldest_src)
            # #4464: evict the decoded renderable in lockstep — an entry
            # missing from `_image_cache` but lingering in
            # `_decoded_image_cache` would be a leak this byte cap exists
            # to prevent (same #3876/#3872 class the module docstring
            # already names for the raw-bytes cache).
            self._decoded_image_cache.pop(oldest_src, None)
            evicted_body = getattr(evicted, "body", b"")
            self._image_cache_bytes -= (
                len(evicted_body) if isinstance(evicted_body, (bytes, bytearray)) else 0
            )

    @property
    def image_cache_size_bytes(self) -> int:
        """Public, snapshot-style read of the current total cached image
        body bytes (#4376) — mirrors ``HookBus.subscriber_count``'s own
        "public, non-private surface for tests/observability" pattern, so
        the bound this class enforces on itself is externally checkable
        without reaching into :attr:`_image_cache` directly."""
        return self._image_cache_bytes

    @property
    def image_cache_byte_cap(self) -> int:
        """Public, read-only view of the total-byte cap this instance
        enforces (#4376) — set once at construction, derived from
        ``image_fetch.DEFAULT_MAX_BYTES``."""
        return self._image_cache_byte_cap

    def has_cached_image(self, src: str) -> bool:
        """Whether *src* currently has a settled resolution in the cache
        (#4376) — the same membership check :meth:`begin_image_resolution`
        uses internally, exposed so a caller (or a test) can observe
        whether an entry survived eviction without reaching into
        :attr:`_image_cache` directly."""
        return src in self._image_cache

    def has_decoded_image(self, src: str) -> bool:
        """Whether *src* currently has a pre-decoded renderable cached
        (#4464) — mirrors :meth:`has_cached_image`'s own public-accessor
        pattern, exposed so a caller (or a test) can observe whether the
        background-thread decode has populated :attr:`_decoded_image_cache`
        without reaching into it directly."""
        return src in self._decoded_image_cache

    def begin_image_resolution(
        self,
        entry: object,
        src: str,
        *,
        allowed_schemes: "list[str] | None" = None,
        on_settled: "Callable[[object], None] | None" = None,
    ) -> None:
        """Kick a background fetch for `src` if it is not already cached or
        in flight (#3846 ②) — the ONE mutation point for :attr:`_image_cache`.

        Fire-and-forget, mirroring ``TextualChatApp._begin_running_indicator``'s
        shape (start something async, let it settle in the background) but
        ONE-SHOT rather than periodic: there is no polling tick here, only a
        single completion callback (``entry.update()``) once the fetch settles
        — an image's byte count does not change moment to moment the way a
        running tool's elapsed time does, so there is nothing to re-tick.

        ``entry`` is untyped here (``object``, not ``Entry[OutboxMessage]``)
        deliberately: this module must not import ``textual_flowview``'s
        ``Entry`` at type-check time for a value it only ever calls
        ``.update()`` on — the caller (``TextualChatApp``, which already
        depends on flowview) passes the real, correctly-typed object; this
        module only needs the one method.

        ``on_settled`` (#4464, default None) is called with ``entry`` once
        THIS resolution settles (ok or failed) — the presenter has no
        ``FlowView`` reference of its own (see this method's own
        ``entry``-untyped rationale above), so it cannot stop the app's own
        running-indicator animation itself; the app supplies this callback
        instead, mirroring how it already supplies ``entry``. Called
        unconditionally alongside ``entry.update()``, from the same
        ``finally`` block.
        """
        if src in self._image_cache or src in self._image_inflight:
            return
        import asyncio

        self._image_inflight.add(src)
        asyncio.create_task(
            self._resolve_image(entry, src, allowed_schemes, on_settled)
        )

    async def _resolve_image(
        self,
        entry: object,
        src: str,
        allowed_schemes: "list[str] | None",
        on_settled: "Callable[[object], None] | None" = None,
    ) -> None:
        import asyncio

        from reyn.core.present.image_fetch import (
            ImageFetchError,
            ImageResolution,
            fetch_image_bytes,
        )

        try:
            body, content_type = await fetch_image_bytes(
                src, allowed_schemes=allowed_schemes
            )
            self._store_image_resolution(
                src, ImageResolution(ok=True, body=body, content_type=content_type)
            )
            # #4464: the CPU-heavy PIL decode + `TextualImage(...)`
            # construction (measured directly: ~100-300ms for a large real
            # photo, versus ~10ms for the sixel-encode `_render_image`'s own
            # inline fallback still does at PAINT time) runs on a background
            # thread — `_render_image` then only has to WRAP the already-
            # built renderable (a cache hit), so no CPU-heavy step is left
            # on the event loop for a resolved image. A decode failure here
            # is silently skipped (not stored as a separate failure state):
            # `_render_image`'s own inline fallback re-attempts the SAME
            # decode on the SAME bytes synchronously and produces the
            # established "[image loaded but could not render: ...]" text
            # — one failure path, not two, for identical bytes.
            try:
                from reyn.interfaces.repl.present_renderer import decode_image_body

                decoded = await asyncio.to_thread(decode_image_body, body)
                self._decoded_image_cache[src] = decoded
            except Exception:
                pass
        except ImageFetchError as exc:
            self._store_image_resolution(src, ImageResolution(ok=False, error=str(exc)))
        except Exception:
            # Cosmetic (a failed image render must never break the pump) —
            # same guard shape as _begin_running_indicator's own try/except.
            import logging

            logging.getLogger(__name__).exception(
                "textual chat: image resolution crashed for %r", src
            )
            self._store_image_resolution(
                src, ImageResolution(ok=False, error="internal error resolving the image")
            )
        finally:
            self._image_inflight.discard(src)
            try:
                entry.update()  # type: ignore[attr-defined]
            except Exception:
                pass
            if on_settled is not None:
                try:
                    on_settled(entry)
                except Exception:
                    pass

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
        the prompt is; a RESTORED answer arrives RAW (persisted RAW by design
        — neutralize only at display boundaries, never at write time), and
        since #3540 a LIVE answer does too (``TextualChatApp.
        _handle_intervention_answer_event`` folds the event's RAW text onto
        this same key, so live and restore store the same bytes). This is
        therefore the ONE real neutralization boundary for an answer label on
        both paths — the panel's own tab-build neutralization covers the
        widget, not this row."""
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

    async def present(self, entry: "Entry[OutboxMessage]", width: int) -> Presentation:
        # #4691 Phase 2: flowview 0.21.0 breaking change — present() now
        # receives the Entry, not the item, so FlowView can carry state
        # (entry.depth, entry.collapsed) without mirroring it into reyn's own
        # item. Unpack immediately; everything below is byte-identical to the
        # pre-0.21.0 body, which always operated on the item.
        item = entry.item
        meta = item.meta or {}
        if item.kind == "intervention":
            return self._present_intervention_pending(item, width)
        if item.kind == "tool_call_started" and meta.get(_RUNNING_SINCE_KEY) is not None:
            return self._present_running_tool(item, width)
        body, background = _body_and_background(
            item,
            neutralize_body=self._neutralize_body,
            image_cache=self._image_cache,
            decoded_image_cache=self._decoded_image_cache,
            now=self._clock(),
        )
        # #4691 Phase B B1 (dogfood follow-up, lead-coder review): a
        # collapsed Group parent must show its child COUNT — "count is
        # information, not design" (lead-coder's own framing). Without
        # this, folding a content-less tool-turn-text row (#4691 ③) left
        # a bare state glyph with NOTHING indicating anything was even
        # hidden — worse than not being able to fold at all, since the
        # owner has no way to tell "nothing here" from "3 rows folded
        # away". Deliberately minimal per the same review's scope: a
        # bare count, no summary wording, no icon vocabulary beyond the
        # existing dim style every other secondary line already uses —
        # the SHAPE of the summary (#4691 issue §5's own "▸ read_file,
        # grep ×2" example) is B2's call, not this fix's.
        if entry.collapsed and entry.children:
            count_line = Text(f"  ({len(entry.children)} folded)", style=_CC_DIM)
            body = Group(body, count_line)
        return Presentation(
            height=self._measure(body, width),
            renderable=body,
            background=background,
        )
