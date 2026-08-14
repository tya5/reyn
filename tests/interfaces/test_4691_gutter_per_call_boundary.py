"""Tier 1: #4691 — an ``agent`` row's per-call figure, embedded in the
row's own meta, on ``ReynTurnUsageGutter``. Since arc item ④ this is the
ONLY thing an agent row ever shows — the turn-total lookup moved off this
kind entirely, onto the ``user`` row (:data:`TURN_TOTAL_ANCHOR_KIND`,
covered in ``test_textual_chat_phase4_turn_cost_gutter_3283.py``), so an
agent row without its own per-call figure now renders an empty cell
rather than falling back to a lookup that used to sit on this same kind.

Root problem (architect's measurement, issue #4691): a turn with more than
one ``kind="agent"`` anchor row (a tool-turn's own explanatory text, an
async spawn ack, an empty-response failure, and the terminal no-tool-calls
reply) used to paint the SAME turn-total figure on every one of them — the
exact "one number N times" duplication ``ReynTurnUsageGutter``'s own
docstring says it exists to avoid, just not caught for the multi-anchor-row
case.

Owner-approved boundary declaration (tui-coder's own 1-line decision,
approved by lead-coder before implementation): the completion boundary IS
the existing row boundary — no new glyph, no Group/nesting.

Both halves are ABSOLUTE — an owner ruling (#4691), not a signed delta.
#4698 tried a signed context-growth delta between consecutive calls first;
the owner's final ruling reversed it: an absolute figure is PRIMITIVE (a
reader can derive a delta by subtracting adjacent rows; a delta can never
recover the absolute value it came from), and #4698's own "9 exception
cases" for a session-shared delta baseline (cross-purpose/cross-session
pollution, /model switches, rewind, fork, compaction) all evaporate with
an absolute figure — only "no usage on the response" (None, never a
fabricated 0) remains. "Showing the jump" between calls is Group/fold's
own job (#4691 Phase B), not this column's.

Real ``FlowModel``/``Entry``/``OutboxMessage`` — no mocks, mirrors
``test_textual_chat_phase4_turn_cost_gutter_3283.py``'s own collaborator
choice for this exact class.
"""
from __future__ import annotations

from textual_flowview import FlowModel

from reyn.interfaces.inline.textual_chat.gutter import (
    RIGHT_GUTTER_WIDTH,
    TURN_ANCHOR_KIND,
    ReynTurnUsageGutter,
)
from reyn.runtime.outbox import OutboxMessage


def _entry(item: OutboxMessage):
    model: "FlowModel[OutboxMessage]" = FlowModel()
    return model.append(item)


def _label(gutter: ReynTurnUsageGutter, item: OutboxMessage) -> str:
    return gutter.decorate(_entry(item), RIGHT_GUTTER_WIDTH, 1).plain.strip()


def test_a_per_call_figure_in_meta_is_rendered_directly() -> None:
    """Tier 1: prompt_tokens/completion_tokens embedded in meta render
    without needing a usage_lookup at all — proving the branch reads the
    row's own data, not a keyed side-channel."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-A", "prompt_tokens": 45890, "completion_tokens": 82},
    )
    assert _label(gutter, row) == "↑46k ↓82"


def test_two_anchor_rows_of_one_turn_no_longer_show_the_same_duplicated_total() -> None:
    """Tier 1: THE core #4691 regression — two ``kind="agent"`` rows sharing
    ONE chain_id (a tool-turn's own text row, then its terminal reply) get
    DIFFERENT figures when each carries its own call's real tokens, instead
    of both reading the same turn-total via the shared chain_id lookup."""
    def _lookup(chain_id: str) -> "dict | None":
        # A turn-total lookup that WOULD paint the same number on both rows
        # if either row fell through to it — the exact pre-#4691 bug shape.
        return {"chain_id": chain_id, "tokens": 999, "prompt_tokens": 900, "completion_tokens": 99}

    gutter = ReynTurnUsageGutter(usage_lookup=_lookup)
    tool_turn_text_row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="let me check that",
        meta={"chain_id": "turn-A", "prompt_tokens": 12000, "completion_tokens": 31},
    )
    terminal_reply_row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="done",
        meta={"chain_id": "turn-A", "prompt_tokens": 45973, "completion_tokens": 46},
    )
    first = _label(gutter, tool_turn_text_row)
    second = _label(gutter, terminal_reply_row)
    assert first == "↑12k ↓31"
    assert second == "↑46k ↓46"
    assert first != second, (
        "two distinct calls in the same turn must render distinct figures — "
        "identical figures here would reproduce the exact duplication bug "
        "#4691 exists to close"
    )
    # Neither row fell through to the shared-total lookup.
    assert "999" not in first and "999" not in second


def test_a_row_with_no_per_call_figure_renders_empty_never_a_turn_total() -> None:
    """Tier 1: #4691 arc item ④ (owner ruling) — a row that carries NO
    per-call meta at all (a restored/legacy frame, or a future agent-kind
    emit site that never threaded one through) renders an EMPTY cell, never
    the chain_id-keyed turn-total lookup. That lookup moved to the ``user``
    row entirely (:data:`TURN_TOTAL_ANCHOR_KIND`) — an agent row with a
    ``chain_id`` but no per-call figures used to silently substitute the
    turn total in the exact same visual slot a genuine per-call figure
    occupies, which is the ambiguity item ④ closes: a reader could not
    tell "this call's own tokens" from "the whole turn's total" without
    already knowing this row's own hidden meta shape. The lookup is
    present here specifically to prove it is NEVER consulted for an agent
    row, not merely unconfigured."""
    def _lookup(chain_id: str) -> "dict | None":
        raise AssertionError(
            f"an agent row must never consult the turn-total lookup, got "
            f"chain_id={chain_id!r}"
        )

    gutter = ReynTurnUsageGutter(usage_lookup=_lookup)
    row = OutboxMessage(kind=TURN_ANCHOR_KIND, text="done", meta={"chain_id": "turn-B"})
    assert _label(gutter, row) == ""


def test_the_owners_originally_reported_symptom_is_closed() -> None:
    """Tier 1: #4691's own regression target (co-vet finding, architect +
    lead-coder) — a turn whose ONLY ``kind="agent"`` row is the terminal
    no-tool-calls reply (every intermediate tool call in it had EMPTY
    content, so router_loop.py's non-terminal tool-turn-text emit never
    fired — the exact shape of the owner's originally-reported turn) must
    render THAT row's own per-call figure, matching ctx tab's absolute
    prompt-window value, rather than falling through to a stale turn-total
    lookup (the owner's original complaint: ctx tab showed 45,973 while the
    row showed a much larger, unrelated 138k turn total)."""
    def _stale_turn_total(chain_id: str) -> "dict | None":
        # If the row fell through to this, it would reproduce the owner's
        # exact reported mismatch — present so a regression is caught, not
        # masked by usage_lookup=None.
        return {"chain_id": chain_id, "tokens": 138_000, "prompt_tokens": 137_954, "completion_tokens": 46}

    gutter = ReynTurnUsageGutter(usage_lookup=_stale_turn_total)
    only_row_in_the_turn = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="done",
        meta={"chain_id": "turn-owner-repro", "prompt_tokens": 45973, "completion_tokens": 46},
    )
    assert _label(gutter, only_row_in_the_turn) == "↑46k ↓46"
    assert "138" not in _label(gutter, only_row_in_the_turn)


def test_a_non_numeric_or_partial_per_call_pair_renders_empty_not_crashes() -> None:
    """Tier 1: meta carrying only ONE of the two fields (a malformed/partial
    emit) is not treated as a valid per-call pair — renders an EMPTY cell
    (#4691 arc item ④: no turn-total fallback exists on an agent row
    anymore) rather than a bogus half-figure or a raised exception."""
    gutter = ReynTurnUsageGutter(usage_lookup=lambda cid: None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-C", "prompt_tokens": 100},  # completion_tokens missing
    )
    assert _label(gutter, row) == ""


def test_a_real_per_call_zero_still_renders_as_a_measured_zero() -> None:
    """Tier 1: a call that genuinely reported 0 prompt/completion tokens
    renders ``↑0 ↓0`` (a fact), not treated as "no figure" — mirrors the
    turn-total lookup path's own real-zero-vs-unknown distinction, now
    extended to the per-call path."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-D", "prompt_tokens": 0, "completion_tokens": 0},
    )
    assert _label(gutter, row) == "↑0 ↓0"
