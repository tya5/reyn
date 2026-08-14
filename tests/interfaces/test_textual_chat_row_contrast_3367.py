"""Row-legibility gate (#3367): no row paints its ink in its own background.

The owner reported failed tool-call rows rendering as a solid coral band with
no readable content. Root cause: ``presenter._body_and_background`` picks a
foreground and a full-row background INDEPENDENTLY, and five failure legs
picked ``_CC_ERR`` for both — the text was drawn in its own background colour.
Because flowview paints ``Presentation.background`` across the gutter column
too (``_view._compose_line``), the left gutter's coral ``⎿``/``✗`` glyph
(``_STATE_COLOR[EntryState.ERROR]``) disappeared with it: ONE background choice
silently decides the legibility of EVERY foreground on that row.

So the gate here is over the PAIRINGS, not over the five legs that were named.
Both foreground producers (``ReynPresenter`` for the body, ``ReynGutter`` for
the gutter glyph) are driven over a cross-product of scenarios whose axes are
ENUMERATED FROM THEIR PRODUCERS rather than hand-listed — a hand-written case
list omits whichever case someone adds next:

- kind ← :data:`reyn.runtime.outbox.DISPLAY_KINDS`, the closed producer
  vocabulary ``OutboxMessage.__post_init__`` validates against;
- settle-state ← ``{None} ∪ DISPLAY_KINDS ∪ {ORPHANED_RESULT_KIND}``, the
  values ``app._coalesce_tool_result`` / ``_sweep_orphaned_running_tools`` /
  ``restore.project_restored_frames`` can stamp under ``RESULT_KIND_KEY``;
- entry state ← ``list(EntryState)``, every member of flowview's enum;
- plus the two payload/meta switches the presenter itself branches on (a
  result summary that is a ``✗`` failure vs. one that is not;
  ``RUNNING_SINCE_KEY`` present vs. absent).

Every scenario's rendered segments are inspected for their EFFECTIVE
(foreground, background) pair and asserted to clear a minimum contrast ratio.
Contrast, not bare inequality: it strictly implies "not the same colour" (an
identical pair scores exactly 1.0) while also catching a near-collision that a
``!=`` check would wave through. The threshold is a floor, not a pin on the
palette's actual values — no test here asserts a specific hex.

All real instances: real ``OutboxMessage`` / ``FlowModel`` / ``Entry`` /
``ReynPresenter`` / ``ReynGutter`` / rich ``Console``. No mocks.

Headless can only establish the colour relationship. The visual confirmation
that a failed tool row now READS in a real terminal is tui-coder's.
"""
from __future__ import annotations

import itertools

import pytest
from rich.color import Color, ColorType
from rich.console import Console, RenderableType
from rich.segment import Segment
from textual_flowview import Entry, EntryState, FlowModel

from reyn.interfaces.inline.textual_chat import ReynGutter, ReynPresenter
from reyn.interfaces.inline.textual_chat._meta_keys import (
    ORPHANED_RESULT_KIND,
    RESULT_KIND_KEY,
    RESULT_META_KEY,
    RUNNING_SINCE_KEY,
)
from reyn.interfaces.repl.renderer import summarize_tool_result
from reyn.runtime.outbox import DISPLAY_KINDS, OutboxMessage

#: Minimum acceptable contrast ratio between a row's ink and the surface it is
#: painted on — a DISTINGUISHABILITY floor, not a WCAG conformance target.
#:
#: Not WCAG AA (4.5): ``_CC_DIM`` is an intentionally ambient, low-contrast
#: colour, and legislating full AA onto it here would turn this gate into a
#: redesign of the palette rather than a defence of the #3367 invariant.
#:
#: #3371 raised this from ``2.0`` to WCAG AA-large's ``3.0``: the worst
#: pairing the gate found, the user row (``_CC_DIM`` on ``_CC_USER_BG``),
#: measured 2.78 — a real legibility gap for the one row that echoes the
#: user's own typed input, not just an ambient log line. Fixed by darkening
#: ``_CC_USER_BG`` (2.78 -> 3.30 measured), not by brightening ``_CC_DIM``
#: itself (which is shared by every ambient/low-importance line elsewhere,
#: outside this widget's background). The floor still separates the #3367
#: failure mode — a foreground painted in its own or a near-identical
#: background (ratio 1.0 exactly when equal) — from every pairing the
#: palette actually produces; the gate asserts strict inequality alongside
#: it. No hex value is pinned anywhere in this file.
MIN_CONTRAST = 3.0

#: A tool result that ``summarize_tool_result`` renders as a ``✗`` failure (the
#: dict-with-``error`` shape). Its failure-ness is ASSERTED in the fixtures
#: below rather than assumed — the ``✗`` prefix is the switch the presenter's
#: ``tool_call_completed`` leg branches on.
_FAILING_RESULT = {"error": "boom"}
_SUCCEEDING_RESULT = {"op": "read", "content": "one\ntwo\n"}

_WIDTH = 60


def _relative_luminance(color: Color) -> float:
    """WCAG relative luminance of a resolved truecolor triplet."""
    triplet = color.get_truecolor()

    def _linear(channel: int) -> float:
        srgb = channel / 255
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = (_linear(c) for c in (triplet.red, triplet.green, triplet.blue))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: Color, bg: Color) -> float:
    """WCAG contrast ratio between two colours — 1.0 when they are identical,
    21.0 for black on white."""
    lighter, darker = sorted(
        (_relative_luminance(fg), _relative_luminance(bg)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _is_concrete(color: "Color | None") -> bool:
    """Whether this is a colour the reyn palette actually CHOSE.

    ``_CC_TEXT`` is the literal string ``"default"`` — the terminal's own
    foreground, resolved by the emulator and unknowable here. A default colour
    can never collide with a chosen one by construction (it is not a value this
    code picks), so it is excluded rather than compared against a guessed RGB.
    """
    return color is not None and color.type is not ColorType.DEFAULT


def _ink_on_surface(
    renderable: RenderableType, row_background: "str | None"
) -> "list[tuple[Color, Color]]":
    """Every visible (foreground, effective-background) pair in ``renderable``.

    The effective background is the segment's OWN background when it sets one
    (a ``Text`` styled ``on <colour>``), else the row background flowview paints
    edge to edge. Whitespace-only segments carry no ink and are skipped.
    """
    console = Console(width=_WIDTH, color_system="truecolor", force_terminal=True)
    row_bg = Color.parse(row_background) if row_background is not None else None
    pairs: list[tuple[Color, Color]] = []
    segment: Segment
    for segment in console.render(renderable, console.options.update_width(_WIDTH)):
        if not segment.text.strip():
            continue
        style = segment.style
        if style is None:
            continue
        background = style.bgcolor if _is_concrete(style.bgcolor) else row_bg
        if not _is_concrete(style.color) or not _is_concrete(background):
            continue
        assert style.color is not None and background is not None  # narrowed above
        pairs.append((style.color, background))
    return pairs


def _settle_kinds() -> "list[str | None]":
    """Every value a producer can stamp under ``RESULT_KIND_KEY`` — enumerated
    from the closed frame vocabulary plus the orphan sentinel, never a
    hand-picked pair of tool kinds."""
    return [None, ORPHANED_RESULT_KIND, *sorted(DISPLAY_KINDS)]


def _scenarios() -> "list[tuple[str, OutboxMessage]]":
    """The (kind, state) cross-product of display frames the presenter + gutter
    can be handed, each as a real :class:`OutboxMessage` with a readable id."""
    rows: list[tuple[str, OutboxMessage]] = []
    for kind, settle_kind, result, running, answered in itertools.product(
        sorted(DISPLAY_KINDS),
        _settle_kinds(),
        (_FAILING_RESULT, _SUCCEEDING_RESULT),
        (False, True),
        (False, True),
    ):
        meta: dict = {
            "tool": "present",
            "args": {"blueprint": {"children": []}},
            "error_message": "bad argument shape",
            "error_kind": "ValidationError",
            # A STANDALONE ``tool_call_completed`` row reads its payload from
            # ``meta["result"]``, while a COALESCED one reads it from
            # ``meta[RESULT_META_KEY]["result"]``. Both are stamped from the same
            # ``result`` so the ``✗``-summary leg is reachable on either path —
            # omitting the top-level key left the standalone leg unexercised, and
            # its strip stayed GREEN.
            "result": result,
        }
        if settle_kind is not None:
            meta[RESULT_KIND_KEY] = settle_kind
            meta[RESULT_META_KEY] = {
                "result": result,
                "error_message": "bad argument shape",
            }
        if running:
            meta[RUNNING_SINCE_KEY] = 0.0
        if answered:
            meta["_answer_label"] = "yes"
        label = (
            f"kind={kind} settled={settle_kind} "
            f"failed_result={result is _FAILING_RESULT} "
            f"running={running} answered={answered}"
        )
        rows.append((label, OutboxMessage(kind=kind, text="something went wrong", meta=meta)))
    return rows


def _entry(msg: OutboxMessage, state: EntryState) -> "Entry[OutboxMessage]":
    """A real flowview :class:`Entry` in ``state`` — the gutter decorator's
    actual argument type, obtained the way production does (from a model)."""
    model: FlowModel[OutboxMessage] = FlowModel()
    entry = model.append(msg)
    entry.set_state(state)
    return entry


def test_scenario_enumeration_is_non_vacuous_and_reaches_the_failure_legs() -> None:
    """Tier 1: the enumeration the contrast gate iterates is non-empty, is
    derived from the producers, and actually reaches the legs that regressed.

    Without this, an enumeration that silently collapsed to zero scenarios — or
    to scenarios that never take a failure branch — would let the contrast gate
    pass by iterating over nothing. Asserts the axes are populated, that the
    failing-result fixture really is what ``summarize_tool_result`` calls a
    failure, and that the three kinds named in #3367 plus the coalesced settle
    states are present in the cross-product.
    """
    scenarios = _scenarios()
    assert scenarios, "scenario enumeration is empty — the contrast gate would be vacuous"
    assert DISPLAY_KINDS, "producer kind vocabulary is empty"
    assert list(EntryState), "flowview EntryState enum is empty"

    # The ``✗`` switch the presenter branches on is real, not assumed.
    assert summarize_tool_result("present", _FAILING_RESULT).startswith("✗")
    assert not summarize_tool_result("present", _SUCCEEDING_RESULT).startswith("✗")

    kinds = {msg.kind for _, msg in scenarios}
    assert {"error", "tool_call_failed", "tool_call_completed", "tool_call_started"} <= kinds
    settled = {(msg.meta or {}).get(RESULT_KIND_KEY) for _, msg in scenarios}
    assert {None, ORPHANED_RESULT_KIND, "tool_call_failed", "tool_call_completed"} <= settled


@pytest.mark.asyncio
async def test_row_ink_is_never_painted_in_its_own_background() -> None:
    """Tier 2b: for every (kind, state) pairing the presenter and gutter can
    emit, every visible foreground clears :data:`MIN_CONTRAST` against the
    surface it is painted on.

    Drives the real ``ReynPresenter.present`` (body + row background) and the
    real ``ReynGutter.decorate`` (glyph, whose colour is ``EntryState``-driven)
    over the full cross-product, and inspects the rendered segments. The gutter
    is included because flowview paints the row background across it too, so a
    background chosen by the presenter governs a foreground chosen by the
    gutter — the coupling that made ONE bad pairing take out the whole row.
    """
    presenter = ReynPresenter(clock=lambda: 0.0)
    gutter = ReynGutter(frame_period=0.0)
    checked = 0
    tinted_kinds: set[str] = set()
    for label, msg in _scenarios():
        presentation = await presenter.present(FlowModel().append(msg), _WIDTH)
        background = presentation.background
        if background is not None:
            tinted_kinds.add(msg.kind)
        renderables: list[RenderableType] = [presentation.renderable]
        for state in EntryState:
            renderables.append(gutter.decorate(_entry(msg, state), 2, 1))
        for renderable in renderables:
            for fg, bg in _ink_on_surface(renderable, background):
                checked += 1
                assert fg.get_truecolor() != bg.get_truecolor(), (
                    f"row ink painted in its own background: {label} — "
                    f"{fg.get_truecolor().hex}"
                )
                ratio = _contrast_ratio(fg, bg)
                assert ratio >= MIN_CONTRAST, (
                    f"illegible row ink: {label} — foreground {fg.get_truecolor().hex} "
                    f"on background {bg.get_truecolor().hex} scores {ratio:.2f}, "
                    f"below the {MIN_CONTRAST} floor"
                )
    assert checked > 0, "no (foreground, background) pair was inspected — gate is vacuous"
    # REACHABILITY, not just non-emptiness: a scenario set that renders plenty of
    # pairs but never drives a row that actually CARRIES a tint would pass the
    # count check above while leaving every collision-capable leg unexercised.
    # This is not hypothetical — the first draft of this file omitted the
    # top-level ``meta["result"]`` key, so the standalone ``tool_call_completed``
    # failure leg was never reached and its strip measured GREEN.
    assert {
        "error",
        "tool_call_failed",
        "tool_call_completed",
        "tool_call_started",
        "user",
    } <= tinted_kinds, f"tinted rows never reached for: {tinted_kinds}"


@pytest.mark.asyncio
async def test_failure_rows_keep_a_distinct_row_tint() -> None:
    """Tier 2b: the fix keeps the failure block-tint rather than dropping it.

    The illegibility could have been "fixed" by removing the background
    entirely, trading a legibility defect for the loss of the visual distinction
    the tint exists to provide. Pins the property instead of the value: a failed
    tool row and an error row still carry SOME row background, and it is not the
    same background an ordinary user row carries.
    """
    presenter = ReynPresenter(clock=lambda: 0.0)
    failed = await presenter.present(
        FlowModel().append(
            OutboxMessage(kind="tool_call_failed", text="boom", meta={"error_message": "boom"}),
        ),
        _WIDTH,
    )
    errored = await presenter.present(
        FlowModel().append(OutboxMessage(kind="error", text="boom")), _WIDTH,
    )
    user = await presenter.present(
        FlowModel().append(OutboxMessage(kind="user", text="hello")), _WIDTH,
    )
    agent = await presenter.present(
        FlowModel().append(OutboxMessage(kind="agent", text="hello")), _WIDTH,
    )

    assert failed.background is not None
    assert errored.background == failed.background
    assert user.background is not None and user.background != failed.background
    assert agent.background is None
