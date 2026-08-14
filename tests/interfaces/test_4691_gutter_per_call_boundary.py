"""Tier 1: #4691 — a per-call figure embedded in the row's own meta wins
over the turn-total lookup on ``ReynTurnUsageGutter``, and (Phase A.5) ↑ is
a SIGNED context-growth delta, not an absolute prompt figure.

Root problem (architect's measurement, issue #4691): a turn with more than
one ``kind="agent"`` anchor row (a tool-turn's own explanatory text, an
async spawn ack, an empty-response failure, and — Phase A.5 — the terminal
no-tool-calls reply too) used to paint the SAME turn-total figure on every
one of them — the exact "one number N times" duplication
``ReynTurnUsageGutter``'s own docstring says it exists to avoid, just not
caught for the multi-anchor-row case.

Owner-approved boundary declaration (tui-coder's own 1-line decision,
approved by lead-coder before implementation): the completion boundary IS
the existing row boundary — no new glyph, no Group/nesting.

Phase A's first attempt put the per-call call's ABSOLUTE
``prompt_tokens`` on ↑. co-vet (architect + lead-coder) found this left
the owner's ORIGINAL reported symptom open (the terminal reply row —
the only anchor row for a turn whose tool calls all had empty content —
was never updated) AND introduced a NEW problem: an absolute per-call
figure duplicates ctx tab's own number in a second place, reproducing the
exact "which number is which" confusion that motivated this issue. Phase
A.5 (owner ruling): ↑ is the SIGNED delta of prompt_tokens between the two
most recent LLM calls this session (``BudgetTracker.
last_context_growth()``) — real per-row information (varies meaningfully
call to call), a sign that can never be mistaken for a total, ``None``
(no prior call to diff against) rendering as unknown rather than a
fabricated ``+0``, and every ``kind="agent"`` emit site (including the
terminal reply) now stamps it.

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


def test_a_per_call_row_renders_signed_growth_and_absolute_completion() -> None:
    """Tier 1: context_growth (signed) + completion_tokens (absolute)
    embedded in meta render without needing a usage_lookup at all — proving
    the new branch reads the row's own data, not a keyed side-channel."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-A", "context_growth": 82, "completion_tokens": 82},
    )
    assert _label(gutter, row) == "↑+82 ↓82"


def test_a_negative_growth_after_compaction_keeps_its_sign() -> None:
    """Tier 1: owner ruling (via lead-coder) — a negative delta (a
    compaction shrank the context) renders WITH its minus sign; the sign is
    never dropped even though the magnitude may lose precision to fit."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-A", "context_growth": -120_000, "completion_tokens": 46},
    )
    assert _label(gutter, row) == "↑-120k ↓46"


def test_two_anchor_rows_of_one_turn_no_longer_show_the_same_duplicated_total() -> None:
    """Tier 1: THE core #4691 regression — two ``kind="agent"`` rows sharing
    ONE chain_id (a tool-turn's own text row, then its terminal reply) get
    DIFFERENT figures when each carries its own call's real growth/
    completion, instead of both reading the same turn-total via the shared
    chain_id lookup."""
    def _lookup(chain_id: str) -> "dict | None":
        # A turn-total lookup that WOULD paint the same number on both rows
        # if either row fell through to it — the exact pre-#4691 bug shape.
        return {"chain_id": chain_id, "tokens": 999, "prompt_tokens": 900, "completion_tokens": 99}

    gutter = ReynTurnUsageGutter(usage_lookup=_lookup)
    tool_turn_text_row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="let me check that",
        meta={"chain_id": "turn-A", "context_growth": 82, "completion_tokens": 31},
    )
    terminal_reply_row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="done",
        meta={"chain_id": "turn-A", "context_growth": 496, "completion_tokens": 46},
    )
    first = _label(gutter, tool_turn_text_row)
    second = _label(gutter, terminal_reply_row)
    assert first == "↑+82 ↓31"
    assert second == "↑+496 ↓46"
    assert first != second, (
        "two distinct calls in the same turn must render distinct figures — "
        "identical figures here would reproduce the exact duplication bug "
        "#4691 exists to close"
    )
    # Neither row fell through to the shared-total lookup.
    assert "999" not in first and "999" not in second


def test_a_row_with_no_completion_figure_falls_back_to_the_turn_total_lookup() -> None:
    """Tier 1: a row that carries NO per-call ``completion_tokens`` at all
    (a restored/legacy frame — every LIVE ``kind="agent"`` emit site now
    stamps one, Phase A.5) still uses the existing chain_id-keyed
    turn-total lookup exactly as before #4691 — this is the ONLY case the
    turn-total lookup answers for now."""
    def _lookup(chain_id: str) -> "dict | None":
        assert chain_id == "turn-B"
        return {"chain_id": chain_id, "tokens": 130, "prompt_tokens": 100, "completion_tokens": 30}

    gutter = ReynTurnUsageGutter(usage_lookup=_lookup)
    row = OutboxMessage(kind=TURN_ANCHOR_KIND, text="done", meta={"chain_id": "turn-B"})
    assert _label(gutter, row) == "↑100 ↓30"


def test_the_owners_originally_reported_symptom_is_closed() -> None:
    """Tier 1: Phase A.5's own regression target (co-vet finding, architect +
    lead-coder) — a turn whose ONLY ``kind="agent"`` row is the terminal
    no-tool-calls reply (every intermediate tool call in it had EMPTY
    content, so router_loop.py's non-terminal tool-turn-text emit never
    fired — the exact shape of the owner's originally-reported turn) must
    render THAT row's own per-call growth/completion, never falling through
    to a stale turn-total lookup (the owner's original complaint: ctx tab
    showed 45,973 while the row showed an unrelated, much larger 138k
    turn total)."""
    def _stale_turn_total(chain_id: str) -> "dict | None":
        # If the row fell through to this, it would reproduce the owner's
        # exact reported mismatch — present so a regression is caught, not
        # masked by usage_lookup=None.
        return {"chain_id": chain_id, "tokens": 138_000, "prompt_tokens": 137_954, "completion_tokens": 46}

    gutter = ReynTurnUsageGutter(usage_lookup=_stale_turn_total)
    only_row_in_the_turn = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="done",
        meta={"chain_id": "turn-owner-repro", "context_growth": 496, "completion_tokens": 46},
    )
    assert _label(gutter, only_row_in_the_turn) == "↑+496 ↓46"
    assert "138" not in _label(gutter, only_row_in_the_turn)


def test_no_baseline_renders_growth_as_unknown_never_a_fabricated_zero() -> None:
    """Tier 1: owner ruling (via lead-coder) — the FIRST call this session
    (or the row right after a restore) has no prior call to diff against.
    ``context_growth`` is ``None`` in that case, and MUST render as unknown
    (``—``), never a fabricated ``+0`` (a real 0 growth is a different,
    measured fact this test does not claim happened). Mirrors the spirit
    of the turn-total lookup's own real-zero-vs-unknown discipline."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-first-ever", "context_growth": None, "completion_tokens": 31},
    )
    assert _label(gutter, row) == f"↑{TURN_USAGE_UNKNOWN} ↓31"


def test_a_missing_completion_figure_falls_back_not_crashes() -> None:
    """Tier 1: meta carrying context_growth but NOT completion_tokens (a
    malformed/partial emit) is not treated as a valid per-call row — falls
    through to the turn-total lookup rather than rendering a bogus
    half-figure or raising."""
    gutter = ReynTurnUsageGutter(usage_lookup=lambda cid: None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-C", "context_growth": 100},  # completion_tokens missing
    )
    assert _label(gutter, row) == TURN_USAGE_UNKNOWN


def test_a_real_zero_completion_still_renders_as_a_measured_zero() -> None:
    """Tier 1: a call that genuinely reported 0 completion tokens renders
    ``↓0`` (a fact), not treated as "no figure" — mirrors the turn-total
    lookup path's own real-zero-vs-unknown distinction, now extended to
    the per-call path."""
    gutter = ReynTurnUsageGutter(usage_lookup=None)
    row = OutboxMessage(
        kind=TURN_ANCHOR_KIND, text="reply",
        meta={"chain_id": "turn-D", "context_growth": 0, "completion_tokens": 0},
    )
    assert _label(gutter, row) == "↑+0 ↓0"
