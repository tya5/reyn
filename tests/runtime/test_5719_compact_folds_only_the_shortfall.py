"""Tier 2: #5719 — ``CompactionController._select_candidates`` folds only as
much of the unprotected middle as is needed to close the shortfall against
``main_M_room``, never "everything between head and tail" unconditionally.

owner real-machine incident (relayed via lead-coder, architect's own naming
of the defect): #5712's fix let a real compaction succeed, but it compacted
1.6M raw_middle chars down to a ~3K summary against a 950K window — the
shortfall was only ~650K, so ~600x more was folded than the window ever
needed freed. head/tail (unchanged since #1128 step 3 — a token-budget
PROTECTION boundary) had been doing double duty as the fold SELECTOR too:
"not protected" and "must be folded" were one predicate. They are now two
steps — head/tail still answer "is this turn protected"; a separate,
shortfall-derived selection (:func:`select_fold_candidates_for_shortfall`,
oldest-first, group-aware, no added slack constant — see that function's
own docstring) answers "of the unprotected turns, how many actually need
to fold".

Real ``CompactionController`` + real ``ChatMessage``/``EventLog``/
``CompactionConfig`` throughout; only the engine (the LLM-call boundary
every other compaction test here also stubs) is a stand-in, and it is
never actually invoked by the tests that assert zero candidates — proving
the shortfall gate short-circuits BEFORE any LLM call would be spent.
"""
from __future__ import annotations

import asyncio

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import (
    SUMMARY_MESSAGE_ROLE,
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    CoversThrough,
    HistoryChunkToCompact,
    select_fold_candidates_for_shortfall,
)
from tests._support.events import collect_events, settle

# head fits 1 turn (50 tokens, "x"*200 via chars4), tail fits 1 turn,
# main_M_room fits exactly 3 turns (150 tokens) of the unprotected middle —
# the shortfall only ever needs to be measured against SOMETHING nonzero,
# not left at 0 the way the sibling suites (whose subject is unrelated to
# this shortfall math) deliberately do.
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=150, effective_trigger=150,
)


class _NeverCalledEngine(CompactionEngine):
    """A stand-in whose ``compact()`` fails the test outright if invoked —
    used by the "shortfall is zero" tests, where the gate must short-
    circuit before any LLM call would be spent."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk, *, covers_through):  # noqa: ANN001
        raise AssertionError(
            "compact() must never be called when the shortfall is <= 0 — "
            "nothing needed folding"
        )


def _emit_compaction_started(
    events: EventLog, input_chunk: HistoryChunkToCompact, covers_through: CoversThrough,
) -> None:
    """Mirrors ``CompactionEngine.compact()``'s own real entry emit — same
    helper shape as the sibling test files in this directory."""
    _summary_messages = [
        m for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
    ]
    events.emit(
        "compaction_started",
        new_message_count=len(input_chunk.messages) - len(_summary_messages),
        covers_through_seq=covers_through if isinstance(covers_through, int) else None,
        covers_through_unavailable_reason=(
            None if isinstance(covers_through, int) else covers_through.value
        ),
        had_previous=bool(_summary_messages),
    )


class _SucceedingEngine(CompactionEngine):
    def __init__(self, events: EventLog) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _emit_compaction_started(self._events, input_chunk, covers_through)
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine, events: EventLog,
) -> "tuple[CompactionController, list, list[ChatMessage]]":
    collected = collect_events(events)
    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers, *, covers_from_seq: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={
                "structured": structured,
                "covers_through_seq": covers,
                "covers_from_seq": covers_from_seq,
            },
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, collected, history


# ---------------------------------------------------------------------------
# select_fold_candidates_for_shortfall — the shared function directly
# ---------------------------------------------------------------------------


def test_shortfall_at_or_below_zero_selects_nothing():
    """Tier 2: acceptance — a shortfall of exactly 0 (or negative) means the
    unprotected middle already fits the window; the correct answer is to
    fold ZERO turns, not "all of them" (the pre-#5719 behavior)."""
    turns = _history(5)
    assert select_fold_candidates_for_shortfall(turns, 0, "", use_chars4=True) == []
    assert select_fold_candidates_for_shortfall(turns, -1, "", use_chars4=True) == []


def test_selection_is_oldest_first_and_stops_once_the_shortfall_is_covered():
    """Tier 2: acceptance ① — oldest-first, stopping at the FIRST point the
    running total reaches the shortfall (never continuing past it, never
    stopping short of it). Each turn here is exactly 50 tokens (chars4);
    a shortfall of 120 needs 3 turns (50+50+50=150 >= 120), not 2
    (50+50=100 < 120) and not 4."""
    turns = _history(6)  # seq 1..6, oldest first
    selected = select_fold_candidates_for_shortfall(turns, 120, "", use_chars4=True)
    assert [t.seq for t in selected] == [1, 2, 3], (
        f"expected the oldest 3 turns (just enough to cover a 120-token "
        f"shortfall at 50 tokens/turn) — got seqs {[t.seq for t in selected]!r}"
    )


def test_selection_never_splits_a_tool_cycle_even_mid_shortfall():
    """Tier 2: group-aware, matching trim_head/trim_tail's own discipline —
    an assistant-with-tool_calls turn and its tool-result turns are ONE
    atomic unit; a shortfall boundary landing inside that unit must still
    take the whole group, never split it (a split would reach the wire as
    a dangling call / orphan result)."""
    turns = [
        ChatMessage(role="user", content="x" * 40, seq=1),  # 10 tokens
        ChatMessage(  # 10 tokens, starts a tool cycle
            role="assistant", content="x" * 40, seq=2,
            tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", content="x" * 400, seq=3, tool_call_id="c1"),  # 100 tokens
        ChatMessage(role="user", content="x" * 40, seq=4),  # 10 tokens
    ]
    # Shortfall of 15 lands INSIDE the tool cycle (10 from seq1, then the
    # 2-turn cycle worth 110 crosses it) — the whole cycle must be taken,
    # never just seq2 alone.
    selected = select_fold_candidates_for_shortfall(turns, 15, "", use_chars4=True)
    assert [t.seq for t in selected] == [1, 2, 3], (
        f"a shortfall boundary inside a tool cycle must take the WHOLE "
        f"cycle, never split it — got seqs {[t.seq for t in selected]!r}"
    )


# ---------------------------------------------------------------------------
# CompactionController._select_candidates — head/tail protection unaffected,
# integrated through the real controller
# ---------------------------------------------------------------------------


def test_head_and_tail_still_protected_even_with_a_large_shortfall():
    """Tier 2: acceptance ② — head/tail's own protection role is untouched
    by #5719: regardless of how large the shortfall is, the newest tail
    turn and the oldest head turn never appear in the candidate set."""
    history = _history(6)  # head=[t1] (50 tok fits head_budget=50), tail=[t6]
    engine = _SucceedingEngine(EventLog())
    ctrl, _collected, _hist = _make_controller(history=history, engine=engine, events=engine._events)

    candidates = ctrl._select_candidates(history, prev_cover=0)

    candidate_seqs = {t.seq for t in candidates}
    assert 1 not in candidate_seqs, "head turn (seq 1) must never be a fold candidate"
    assert 6 not in candidate_seqs, "tail turn (seq 6) must never be a fold candidate"


def test_middle_that_already_fits_the_window_folds_nothing():
    """Tier 2: acceptance ① — the real-machine incident's own shape: a
    middle band that already fits main_M_room must produce ZERO candidates
    and never call compact() at all (the pre-#5719 code folded it anyway).
    """
    history = _history(4)  # head=[t1], tail=[t4], middle=[t2,t3]=100 tok < 150
    engine = _NeverCalledEngine()
    ctrl, collected, _hist = _make_controller(history=history, engine=engine, events=engine._events)

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.candidate_count == 0
    assert not result.failed
    started = [e for e in collected if e.type == "compaction_started"]
    assert not started, "compact() must never be reached when the shortfall is <= 0"


def test_only_the_oldest_shortfall_covering_turns_fold_the_rest_survive():
    """Tier 2: acceptance ① end-to-end — the real-machine shape reproduced
    through ``force_compact_now`` itself, not just the selector function
    directly: a middle band LARGER than main_M_room folds only its oldest
    turns (just enough to close the shortfall) — the newer, un-folded
    middle turns remain individually in history, never swept into the
    summary just because they were "between head and tail"."""
    # head=[t1] (50 tok), tail=[t10] (50 tok), middle=t2..t9 (8 turns, 400
    # tok) vs. main_M_room=150 -> shortfall=250 -> needs ceil(250/50)=5
    # oldest middle turns (t2..t6, 250 tok) to close it; t7..t9 survive.
    history = _history(10)
    events = EventLog()
    engine = _SucceedingEngine(events)
    ctrl, collected, hist = _make_controller(history=history, engine=engine, events=events)

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))
    asyncio.run(settle(events))

    assert not result.failed
    assert result.candidate_count == 5, (
        f"expected exactly the oldest 5 middle turns to close a 250-token "
        f"shortfall at 50 tokens/turn — got {result.candidate_count}"
    )
    started = [e for e in collected if e.type == "compaction_started"]
    assert started, "expected a real compact() call"
    assert started[0].data.get("covers_through_seq") == 6, (
        "the summary must cover only through the oldest folded turn (seq "
        f"6) — got {started[0].data.get('covers_through_seq')!r}"
    )
    # t7..t9 (never folded, never covered) must still be present as
    # individual turns in history — never silently dropped.
    surviving_seqs = {m.seq for m in hist if m.role not in ("summary",)}
    assert {7, 8, 9} <= surviving_seqs, (
        f"turns beyond the shortfall must survive un-folded — got {surviving_seqs!r}"
    )
