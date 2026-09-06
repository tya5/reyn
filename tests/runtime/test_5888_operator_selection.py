"""Tier 2: #5888 — an operator's explicit ``/compact`` folds the whole
unprotected middle; the reactive ladder still folds only the shortfall.

owner real-machine report (2026-09-07, verbatim): "ctx 75% なのに下記メッセ
ージ出るのも謎。ユーザは圧縮したいのにできない。/ Nothing was compacted this
pass, and the window is still full (~0 tokens free) … There was nothing
eligible to fold."

Two facts were wrong in that one reply, and this file pins the half that
lives in ``CompactionController``:

- **Nothing folded.** ``_select_candidates`` selected against
  ``shortfall = unprotected_tokens - main_M_room`` (#5719), which is
  ``<= 0`` whenever the unprotected middle already fits — so an explicit
  request to SHRINK selected zero. #5719's shortfall rule exists to stop
  the REACTIVE ladder's own 600x over-fold; it was never a reason to
  refuse an operator. ``selection="operator"`` now folds all of the
  unprotected, uncovered middle, and ``selection="shortfall"`` (the
  default every other caller keeps) is unchanged — the positive control
  below is #5719's own non-regression.
- **"nothing eligible to fold" was false.** Entries WERE eligible;
  head/tail protection held every one of them back. ``ForceCompactResult``
  now carries ``eligible_count`` alongside ``candidate_count`` so the two
  states stop sharing a sentence (the ``/compact`` rendering half is
  pinned in ``tests/interfaces/test_slash_compact_cmd.py``).

Real ``CompactionController`` + real ``ChatMessage``/``EventLog``/
``CompactionConfig`` throughout; only the engine (the LLM-call boundary
every other compaction test in this directory also stubs) is a stand-in,
and the reactive-control test uses one that FAILS if it is ever invoked —
proving the shortfall gate short-circuits before any LLM call is spent.
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
)

# Same stub budgets as tests/runtime/test_5719_compact_folds_only_the_
# shortfall.py: head fits 1 turn (50 tokens via chars4), tail fits 1, and
# main_M_room fits 3 turns of unprotected middle. A 4-turn history is then
# exactly the owner's shape — head + tail + a middle that ALREADY fits.
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=150, effective_trigger=150,
)


class _NeverCalledEngine(CompactionEngine):
    """A stand-in whose ``compact()`` fails the test outright if invoked —
    the reactive control's own witness that a fitting middle spends no LLM
    call at all."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(self, input_chunk, *, covers_through):  # noqa: ANN001
        raise AssertionError(
            "compact() must never be called under selection='shortfall' when "
            "the unprotected middle already fits — nothing needed folding"
        )


class _SucceedingEngine(CompactionEngine):
    def __init__(self, events: EventLog) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _summary_messages = [
            m for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
        ]
        self._events.emit(
            "compaction_started",
            new_message_count=len(input_chunk.messages) - len(_summary_messages),
            covers_through_seq=covers_through if isinstance(covers_through, int) else None,
            covers_through_unavailable_reason=(
                None if isinstance(covers_through, int) else covers_through.value
            ),
            had_previous=bool(_summary_messages),
        )
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine, events: EventLog,
) -> CompactionController:
    return CompactionController(
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


def test_operator_selection_folds_the_whole_unprotected_middle() -> None:
    """Tier 2: #5888 accept ② — with a middle that ALREADY FITS
    ``main_M_room`` (shortfall <= 0, the owner's own shape), an operator's
    request folds every unprotected, uncovered turn.

    strip witness: dropping the ``selection == "operator"`` branch in
    ``_measure_and_select`` sends this back through
    ``select_fold_candidates_for_shortfall``, which returns ``[]`` for a
    non-positive shortfall — candidate_count becomes 0 and this goes red
    (verified directly during this fix)."""
    history = _history(4)  # head=[t1], tail=[t4], middle=[t2,t3] = 100 tok < 150
    events = EventLog()
    ctrl = _make_controller(
        history=history, engine=_SucceedingEngine(events), events=events,
    )

    result = asyncio.run(
        ctrl.force_compact_now(spill_fn=lambda _candidates: [], selection="operator"),
    )

    assert not result.failed
    assert result.selection == "operator"
    assert result.candidate_count == 2, (
        "an explicit operator request must fold the whole unprotected middle "
        f"(t2, t3) even though it already fits — got {result.candidate_count}"
    )


def test_reactive_shortfall_selection_still_folds_nothing_when_the_middle_fits() -> None:
    """Tier 2: #5888 accept ② positive control — the SAME history, through
    the default ``selection="shortfall"``, still folds nothing and spends
    no LLM call. #5719's own rule (fold only the shortfall, never "all of
    it") is untouched for the reactive ladder; only the operator's own
    request changed.

    Paired deliberately with the test above: the contrast between the two
    IS the acceptance item — a change that made operator mode work by
    widening selection for EVERYONE would pass that one and fail this
    one."""
    history = _history(4)
    engine = _NeverCalledEngine()
    ctrl = _make_controller(history=history, engine=engine, events=engine._events)

    result = asyncio.run(ctrl.force_compact_now(spill_fn=lambda _candidates: []))

    assert result.selection == "shortfall"
    assert result.candidate_count == 0, (
        "the reactive ladder must still fold only the shortfall (#5719) — "
        f"got {result.candidate_count}"
    )
    assert not result.failed


def test_all_eligible_but_all_protected_is_distinguishable_from_nothing_eligible() -> None:
    """Tier 2: #5888 accept ③ (controller half) — a history whose head and
    tail protect EVERY eligible entry reports ``eligible_count > 0`` with
    ``candidate_count == 0``, so a caller can say "all of it is protected"
    instead of "nothing was eligible".

    Those were the same reply before this change, and the owner got the
    wrong one: their history was full of foldable content, and ``/compact``
    told them there was none.

    strip witness: removing ``eligible_count`` from ``ForceCompactResult``
    (or leaving it at its default here) collapses the two states back into
    one and this goes red."""
    # 2 turns of 50 tokens each: head takes t1 (head_budget=50), tail takes
    # t2 (tail_budget=50) — nothing is left unprotected at all.
    history = _history(2)
    engine = _NeverCalledEngine()
    ctrl = _make_controller(history=history, engine=engine, events=engine._events)

    result = asyncio.run(
        ctrl.force_compact_now(spill_fn=lambda _candidates: [], selection="operator"),
    )

    assert result.outcome == "forced_sync", (
        "entries WERE eligible — this must not report as forced_sync_no_turns, "
        f"got {result.outcome!r}"
    )
    assert result.eligible_count == 2, (
        f"expected both turns counted as eligible; got {result.eligible_count}"
    )
    assert result.candidate_count == 0, (
        "head/tail protected both turns, so nothing could be folded — got "
        f"{result.candidate_count}"
    )


def test_the_result_carries_the_quantities_the_pass_measured() -> None:
    """Tier 2: #5888 accept ① (controller half) — the numbers ``/compact``
    prints come from the SAME selection pass that decided what to fold,
    not from a second computation that could drift from it.

    ``middle room``/``used`` and the head/tail protection totals are what
    the pre-#5888 reply had no access to at all — its only number was
    ``free_window_after``, which it then described as the model's context
    window (the category error the owner saw)."""
    history = _history(4)
    events = EventLog()
    ctrl = _make_controller(
        history=history, engine=_SucceedingEngine(events), events=events,
    )

    result = asyncio.run(
        ctrl.force_compact_now(spill_fn=lambda _candidates: [], selection="operator"),
    )

    assert result.middle_room_tokens == 150, (
        f"middle room must be main_M_room; got {result.middle_room_tokens}"
    )
    assert result.middle_used_tokens == 100, (
        "the unprotected middle here is t2+t3 at 50 tokens each; got "
        f"{result.middle_used_tokens}"
    )
    assert result.protected_head_tokens == 50 and result.protected_tail_tokens == 50, (
        f"head/tail each protect one 50-token turn; got "
        f"{result.protected_head_tokens}/{result.protected_tail_tokens}"
    )
    assert result.protected_head_turns == 1 and result.protected_tail_turns == 1
    assert result.covers_through_seq == 0, (
        "no previous summary in this fixture, so nothing is covered yet; got "
        f"{result.covers_through_seq}"
    )


def test_overlapping_head_and_tail_windows_are_not_double_counted() -> None:
    """Tier 2: #5888 — on a history short enough that BOTH protection
    windows cover every turn, the reported protected turns/tokens count
    each turn once, not twice.

    Found by running the real `/compact` path end to end (`tests/runtime/
    test_slash_compact_191.py`): a genuine 3-turn session rendered
    "protected: head 3 + tail 3 tokens (6 turns)" — 6 turns out of 3,
    because `trim_head` and `trim_tail` both returned all three and the
    two were summed. A number a reply prints has to survive being read;
    "6 turns" in a 3-turn conversation is the same class of wrong as the
    window sentence this whole issue is about, one field over.

    An overlapping turn is attributed to HEAD (computed first), so the
    two figures stay disjoint and their sum is exactly what is protected.

    strip witness: reporting `len(tail_messages)` instead of the
    head-excluded `tail_only` makes this read 4 turns for a 2-turn
    history and goes red."""
    history = _history(2)  # both windows cover both turns at these budgets
    engine = _NeverCalledEngine()
    ctrl = _make_controller(history=history, engine=engine, events=engine._events)

    result = asyncio.run(
        ctrl.force_compact_now(spill_fn=lambda _candidates: [], selection="operator"),
    )

    protected_turns = result.protected_head_turns + result.protected_tail_turns
    assert protected_turns == 2, (
        "a 2-turn history cannot have more than 2 protected turns — the head "
        f"and tail windows overlap and were summed; got {protected_turns}"
    )
    assert result.protected_head_tokens + result.protected_tail_tokens == 100, (
        "protected tokens must likewise count each turn once (2 x 50); got "
        f"{result.protected_head_tokens} + {result.protected_tail_tokens}"
    )
