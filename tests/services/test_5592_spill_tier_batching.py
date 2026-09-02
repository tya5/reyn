"""Tier 2: #5592 — retry_loop's rung① (spill) sends a WHOLE Spillability
tier per request, instead of one raw_middle candidate per request, turning
an O(N) compaction call count into O(1) per overflow (final design;
supersedes this file's own withdrawn doubling-batch draft — see the module
history below for the churn).

Owner's real machine (relayed by lead-coder, #5592): 2469 raw_middle
candidates, ~6 seconds per compact() call (a real LLM call per spilled
candidate under the pre-#5592 one-at-a-time rung①) — an estimated ~4.1
hours to fully recover from one overflow.

Design history (disclosed — this arc churned 3+ times before landing;
architect's own "#5592の発注文は面の最新の全文置換が正典です" comment is the
source of truth this file now implements):
  1. Architect's original doubling-batch proposal (1,2,4,8...) — WITHDRAWN
     by architect itself: "owner's whole-tier batch is better than my
     doubling — what's expensive is the CALL COUNT, not the over-spilled
     bytes" (owner: rejected requests are still billed and unobservable
     from inside reyn, so 12 calls vs 2 calls is a real cost difference,
     not just a latency one).
  2. FINAL (owner-ruled, this file): one request per (face × Spillability
     tier) — ``FIRST_CHOICE`` entirely in one request; if that alone does
     not resolve the overflow, ``LAST_RESORT`` entirely in a second.
     ``chat.compaction.spill_granularity: tier | turn`` (default
     ``tier``) is the ONE config knob — ``turn`` reproduces the pre-#5592
     one-candidate-per-request behavior exactly (same algorithm, step
     size K=1 instead of "the whole tier"), never a second code path
     (architect: "if で2経路を作らないでください — 2経路は2回testされ片方だ
     け腐ります").
  3. Population (母数) is explicitly UNCHANGED by this whole arc (owner:
     "spill 母数の定義は変えないでね") — ``levers_left``/
     ``compaction_shrink_recovered``'s own remaining-candidate count is
     the untouched raw_middle candidate count; only how many REQUESTS it
     takes to consume that population changes.

Falsified before writing (lead-coder's final 3-point ask, #5592
issuecomment, differs from the withdrawn doubling design's own 3 points):
① Is ``Spillability`` genuinely 3-valued (FIRST_CHOICE/LAST_RESORT/NEVER),
   default LAST_RESORT? Read directly: ``chat_message.py``'s own enum body
   and its ``default()`` classmethod — confirmed.
② Is ``NEVER`` guaranteed excluded from either tier when sending per-tier?
   Read directly: ``_spill_batch_within_face``'s own ``_eligible`` filter
   (router_loop_driver.py) checks ``spillability != NEVER.value`` before
   any tier bucketing happens — structural, not per-tier logic that could
   miss it.
③ Where are the remaining-candidate count (③) and upstream-call count (⑤)
   produced? Read directly: ``compaction_shrink_recovered``'s own
   ``raw_middle_remaining``/``raw_middle_total`` fields (engine.py, single
   producer, #5588's own ``levers_left`` reads the SAME expression) and
   ``llm.py``'s own ``note_upstream_recovery_call_attempt``/
   ``_current_upstream_recovery_call_count`` (single producer, a
   ContextVar-backed counter retry_loop increments once per upstream
   call — never re-derived at the emission site).

Accept: N candidates in the SAME Spillability tier consume 1 compact()
call to spill, not N.
Deny ①: a single candidate (N=1) still resolves in the pre-#5592 minimum
call count (batching adds no fixed overhead to the smallest case).
Deny ②: when spill alone cannot resolve the overflow, MID_FLOOR is still
reached — tier-batching does not mask the genuine terminal.
Deny ③: ``spill_granularity: "turn"`` reproduces the pre-#5592
one-candidate-per-call behavior exactly (same algorithm, K=1).

Real ``retry_loop``/``TokenMultiplierLearner`` throughout (no mocks) — same
idiom ``test_pr_n6_compaction_overflow_retry.py``'s own
``_SpillableByteLimitEngine``/``_spill_fn`` fixtures already establish for
this exact function.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from reyn.config import CompactionConfig
from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
from reyn.services.compaction.engine import (
    ChatSummary,
    RetryLoopTerminal,
    RetryPayload,
    UnrecoveredError,
    retry_loop,
)


def _make_cfg(**kwargs) -> CompactionConfig:
    defaults: dict = dict(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    defaults.update(kwargs)
    return CompactionConfig(**defaults)


def _tier_tagged_raw_middle(n: int, *, spillability: str = "first_choice") -> list[dict]:
    return [
        {
            "role": "tool", "content": f"OVERSIZED_{i}", "seq": i + 1,
            "tool_call_id": f"tc-{i}", "name": "big_tool",
            "spillability": spillability,
        }
        for i in range(n)
    ]


async def _main_call(**kwargs):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10), choices=[])


class _FakeStatusError(Exception):
    """A minimal stand-in for openai.APIStatusError's own shape (a plain
    ``status_code`` attribute), same helper shape
    ``test_pr_n6_compaction_overflow_retry.py`` already establishes for
    this exact fixture family — defined locally rather than imported
    cross-file (tests must not depend on another test module's private
    helpers)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _make_budgets():
    from reyn.services.compaction.engine import ComputedBudgets
    return ComputedBudgets(
        main_pool=10_000, head_budget=1_000, body_budget=500,
        tail_budget=1_500, new_msg_budget=1_000,
        B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
        section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                      "session_user_facts": 50, "artifacts_referenced": 175},
    )


class _AllMustBeSpilledEngine:
    """compact() succeeds ONLY once every offered turn's content has
    already been replaced by a spilled marker (no ``"OVERSIZED_"`` prefix
    remaining) — the same "spill unblocks the retry" shape
    ``test_pr_n6_compaction_overflow_retry.py``'s own
    ``_SpillableByteLimitEngine`` uses, generalized to N candidates."""

    def __init__(self) -> None:
        from reyn.core.events.events import EventLog
        self.compact_calls = 0
        self.budgets = _make_budgets()
        self._events = EventLog()

    async def compact(self, input_chunk, *, covers_through=None):
        self.compact_calls += 1
        if any(
            isinstance(t.get("content"), str) and t["content"].startswith("OVERSIZED_")
            for t in input_chunk.messages
        ):
            raise _FakeStatusError("compact 413", status_code=413)
        return ChatSummary(topic_arc="ok", covers_through_seq=1)


def _tier_batch_spill_fn(candidates: "list[dict]") -> "list[tuple[int, dict]]":
    """#5592's own final contract — spill the WHOLE current tier in one
    call: every ``first_choice`` candidate first; only once none remain
    does the caller (this fixture never decides that — it just reports
    what IS spillable this call) offer ``last_resort`` ones instead. This
    fixture never mixes tiers in one returned batch, matching
    ``_spill_batch_within_face``'s own real behavior."""
    first_choice = [
        (i, t) for i, t in enumerate(candidates)
        if isinstance(t.get("content"), str) and t["content"].startswith("OVERSIZED_")
        and t.get("spillability") == "first_choice"
    ]
    pool = first_choice or [
        (i, t) for i, t in enumerate(candidates)
        if isinstance(t.get("content"), str) and t["content"].startswith("OVERSIZED_")
        and t.get("spillability") != "first_choice"
    ]
    return [
        (i, {**t, "content": f"REF: spilled {t['content']}"}) for i, t in pool
    ]


def _turn_granularity_spill_fn(candidates: "list[dict]") -> "list[tuple[int, dict]]":
    """Pre-#5592 shape reproduced under the new list-returning contract —
    exactly ONE candidate per call, largest-index-first search order
    (irrelevant here, all candidates are equal size)."""
    for i, t in enumerate(candidates):
        if isinstance(t.get("content"), str) and t["content"].startswith("OVERSIZED_"):
            return [(i, {**t, "content": f"REF: spilled {t['content']}"})]
    return []


def test_tier_batch_consumes_15_candidates_in_2_compact_calls() -> None:
    """Tier 2: #5592 accept — 15 candidates, ALL in the SAME
    (``first_choice``) tier. Pre-#5592 (one-at-a-time): 15 failing calls +
    1 succeeding = 16. Post-#5592 (whole-tier-per-request): the entire
    tier spills in ONE request — 1 failing call (everything still
    oversized) + 1 succeeding call = exactly 2, independent of N. The
    exact count (not merely "fewer") is the point: this is what "1 request
    per tier" actually looks like for this N."""
    cfg = _make_cfg()
    engine = _AllMustBeSpilledEngine()
    learner = TokenMultiplierLearner(storage_path=None)  # in-memory only

    raw_middle = _tier_tagged_raw_middle(15)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    result = asyncio.run(retry_loop(
        SP="sp", payload=RetryPayload(
            head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg,
            seq_by_id={},
        ), cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_main_call,
        spill_fn=_tier_batch_spill_fn,
    ))

    assert result is not None, "retry_loop must return normally, not raise"
    assert engine.compact_calls == 2, (
        f"expected exactly 2 compact() calls (whole tier spills in 1 "
        f"request, regardless of N=15) — got {engine.compact_calls}"
    )


def test_single_candidate_still_resolves_in_the_minimum_call_count() -> None:
    """Tier 2: #5592 deny ① — N=1 (the minimum case) is unaffected by
    batching: exactly 2 compact() calls (1 failing on the original
    content, 1 succeeding on the spilled content), matching
    ``test_pr_n6_compaction_overflow_retry.py``'s own pre-existing
    single-turn spill test byte-for-byte. Rules out an implementation
    that adds fixed batching overhead even to the smallest case."""
    cfg = _make_cfg()
    engine = _AllMustBeSpilledEngine()
    learner = TokenMultiplierLearner(storage_path=None)

    raw_middle = _tier_tagged_raw_middle(1)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    result = asyncio.run(retry_loop(
        SP="sp", payload=RetryPayload(
            head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg,
            seq_by_id={},
        ), cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_main_call,
        spill_fn=_tier_batch_spill_fn,
    ))

    assert result is not None
    assert engine.compact_calls == 2, (
        f"expected exactly 2 compact() calls for the N=1 minimum case "
        f"— got {engine.compact_calls}"
    )


def test_when_spill_alone_cannot_resolve_it_mid_floor_is_still_reached() -> None:
    """Tier 2: #5592 deny ② — a raw_middle whose content spill cannot
    shrink enough to fit (compact() keeps 413ing even after every
    candidate is spilled) still reaches MID_FLOOR — tier-batching does
    not mask or delay the genuine terminal condition rung②/the floor
    check is responsible for."""
    cfg = _make_cfg()

    class _NeverSucceedsEngine:
        def __init__(self) -> None:
            from reyn.core.events.events import EventLog
            self.compact_calls = 0
            self.budgets = _make_budgets()
            self._events = EventLog()

        async def compact(self, input_chunk, *, covers_through=None):
            self.compact_calls += 1
            raise _FakeStatusError("compact 413", status_code=413)

    engine = _NeverSucceedsEngine()
    learner = TokenMultiplierLearner(storage_path=None)
    raw_middle = _tier_tagged_raw_middle(3)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    try:
        asyncio.run(retry_loop(
            SP="sp", payload=RetryPayload(
            head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg,
            seq_by_id={},
        ), cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_main_call,
            spill_fn=_tier_batch_spill_fn,
        ))
        raise AssertionError("expected UnrecoveredError, retry_loop returned normally")
    except UnrecoveredError as exc:
        assert exc.terminal is RetryLoopTerminal.MID_FLOOR, (
            f"expected MID_FLOOR terminal, got {exc.terminal!r}"
        )


def test_spill_fn_returning_empty_list_falls_through_to_halving() -> None:
    """Tier 2: #5592 accept (contract shape) — an empty ``list`` from
    ``spill_fn`` (the new "nothing left to spill" signal, replacing the
    old ``None``) makes rung① a no-op and rung② (``_compact_attempt_len``
    halving) fire on the VERY NEXT iteration — never an infinite loop on
    an empty-but-non-``None`` batch."""
    cfg = _make_cfg()
    engine = _AllMustBeSpilledEngine()
    learner = TokenMultiplierLearner(storage_path=None)
    raw_middle = _tier_tagged_raw_middle(2)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    def _never_spills(_candidates: "list[dict]") -> "list[tuple[int, dict]]":
        return []

    try:
        asyncio.run(retry_loop(
            SP="sp", payload=RetryPayload(
            head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg,
            seq_by_id={},
        ), cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_main_call,
            spill_fn=_never_spills,
        ))
        raise AssertionError("expected UnrecoveredError, retry_loop returned normally")
    except UnrecoveredError as exc:
        # #5364/#5531 §10: halving eventually floors at attempt=1 (a
        # single, still-oversized turn) — MID_FLOOR, not a hang.
        assert exc.terminal is RetryLoopTerminal.MID_FLOOR


def test_spill_granularity_turn_reproduces_one_candidate_per_call() -> None:
    """Tier 2: #5592 deny ③ — ``spill_granularity: "turn"`` (the escape
    hatch, not the default) sends exactly ONE candidate per request, so N
    candidates take N failing calls + 1 succeeding — the pre-#5592
    behavior byte-for-byte, proving ``turn`` really is "the same
    algorithm, step size 1" and not a separately-implemented path (which
    could silently drift from what ``tier`` shares with it)."""
    cfg = _make_cfg(spill_granularity="turn")
    engine = _AllMustBeSpilledEngine()
    learner = TokenMultiplierLearner(storage_path=None)
    raw_middle = _tier_tagged_raw_middle(4)
    new_msg = {"role": "user", "content": "q", "seq": 999}

    result = asyncio.run(retry_loop(
        SP="sp", payload=RetryPayload(
            head=[], raw_middle=raw_middle,
            tail=[], new_msg=new_msg,
            seq_by_id={},
        ), cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_main_call,
        spill_fn=_turn_granularity_spill_fn,
    ))

    assert result is not None
    assert engine.compact_calls == 5, (
        f"expected exactly 5 compact() calls (4 candidates, one-at-a-time "
        f"= 4 failures + 1 success) — got {engine.compact_calls}"
    )
