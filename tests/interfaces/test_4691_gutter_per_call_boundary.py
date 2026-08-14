"""Tier 1: #4691 — a per-call figure embedded in the row's own meta wins
over the turn-total lookup on ``ReynTurnUsageGutter``.

Root problem (architect's measurement, issue #4691): a turn with more than
one ``kind="agent"`` anchor row (a tool-turn's own explanatory text +
its terminal reply, an async spawn ack, an empty-response failure) used to
paint the SAME turn-total figure on every one of them — the exact
"one number N times" duplication ``ReynTurnUsageGutter``'s own docstring
says it exists to avoid, just not caught for the multi-anchor-row case.

Owner-approved fix (issue #4691, tui-coder's own approved 1-line
declaration): the completion boundary IS the existing row boundary — no
new glyph, no Group/nesting. Each such row now carries its OWN call's
real ``prompt_tokens``/``completion_tokens`` in ``entry.item.meta``
(known synchronously at the ``put_outbox`` call site — see
``router_loop.py``'s ``kind="agent"`` emit sites), and the gutter prefers
that over the ``chain_id``-keyed turn-total lookup when present. A row
with two adjacent, genuinely different call figures self-evidently marks
two distinct completions.

Real ``FlowModel``/``Entry``/``OutboxMessage`` — no mocks, mirrors
``test_textual_chat_phase4_turn_cost_gutter_3283.py``'s own collaborator
choice for this exact class.
"""
from __future__ import annotations

from textual_flowview import FlowModel

from reyn.interfaces.inline.textual_chat.gutter import (
    RIGHT_GUTTER_WIDTH,
    TURN_ANCHOR_KIND,
    TURN_USAGE_UNKNOWN,
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
    without needing a usage_lookup at all — proving the new branch reads
    the row's own data, not a keyed side-channel."""
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


def test_a_row_with_no_per_call_figure_still_falls_back_to_the_turn_total_lookup() -> None:
    """Tier 1: the terminal no-tool-calls reply — the ONLY anchor row in an
    ordinary single-call turn — is UNCHANGED: with no per-call meta fields,
    it still uses the existing chain_id-keyed turn-total lookup exactly as
    before #4691 (this file's own scoping decision: only rows that carry
    the new fields opt into per-call rendering; nothing that doesn't carry
    them regresses)."""
    def _lookup(chain_id: str) -> "dict | None":
        assert chain_id == "turn-B"
        return {"chain_id": chain_id, "tokens": 130, "prompt_tokens": 100, "completion_tokens": 30}

    gutter = ReynTurnUsageGutter(usage_lookup=_lookup)
    row = OutboxMessage(kind=TURN_ANCHOR_KIND, text="done", meta={"chain_id": "turn-B"})
    assert _label(gutter, row) == "↑100 ↓30"


def test_a_non_numeric_or_partial_per_call_pair_falls_back_not_crashes() -> None:
    """Tier 1: meta carrying only ONE of the two fields (a malformed/partial
    emit) is not treated as a valid per-call pair — falls through to the
    turn-total lookup rather than rendering a bogus half-figure or raising."""
    gutter = ReynTurnUsageGutter(usage_lookup=lambda cid: None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-C", "prompt_tokens": 100},  # completion_tokens missing
    )
    assert _label(gutter, row) == TURN_USAGE_UNKNOWN


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
