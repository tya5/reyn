"""Tier 2: ContextBudgetAdvisor incremental history-token cache (#2940).

The status-bar ctx chip's dropdown calls context_window_status() every time
it opens, and maybe_force_compact() calls the same estimate every pre-frame
overflow check. Both previously re-ran json.dumps(FULL history) + estimate_
tokens on every single call — O(history size) each time, growing unbounded
over a long session (the reported freeze). _incremental_history_tokens()
caches (history length, cumulative estimate) and only estimates the NEW tail
slice on growth, returning the cached total unchanged (O(1)) when history
hasn't grown, and fully recomputing (not silently returning stale data) when
history SHRINKS (compaction/rewind truncated it).

#2957 PR-B: the per-call estimate switched from
``estimate_tokens(json.dumps(combined_or_delta_slice))`` to summing
``estimate_tokens_for_any_turn`` PER TURN — the same canonical wire-dict-shaped
per-turn quantity RouterHistoryBuffer's elide-threshold check now measures
(closing a prior circularity: elide measured pre-serialise ChatMessage while
this advisor measured post-serialise json.dumps, two different quantities
for the same conversation; json.dumps additionally counted an inlined
image's full base64 payload as text instead of the fixed per-image cost).
Because per-turn summation is associative (no cross-turn JSON-array
serialisation), the incremental cache's running total is now mathematically
IDENTICAL to a from-scratch per-turn sum over the same history — no
tokenizer merge-boundary drift is possible, unlike the pre-PR-B combined-
string scheme this file used to have to bound with an empirical tolerance.

Real ContextBudgetAdvisor + real estimate_tokens_for_any_turn — no mocks. The
estimate function itself is exact-deterministic for a given turn (no LLM
call), so correctness can be checked by comparing the incremental result
against a from-scratch per-turn sum over the same final history.
"""
from __future__ import annotations

import json

from reyn.config import CompactionConfig
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.services.compaction.engine import estimate_tokens_for_any_turn


def _make_advisor(history_fn, *, model: str = "openai/gpt-4o") -> ContextBudgetAdvisor:
    return ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=None,
        media_store=None,
        model_fn=lambda: model,
        events=None,
        history_fn=history_fn,
    )


def _from_scratch_tokens(history: list, model: str) -> int:
    return sum(estimate_tokens_for_any_turn(m, model, use_chars4=False) for m in history)


def test_unchanged_history_skips_estimate_entirely(monkeypatch) -> None:
    """Tier 2: falsifying — proves the cache HIT path, not just that its
    output happens to match (a real spy on estimate_tokens_for_any_turn, per the
    #2937 counting-spy idiom — a real callable recording per-turn call
    counts in a dict, not a MagicMock, and not a bare len(list)==N format
    pin). Reopening the dropdown with no new messages since the last call
    must not re-estimate at all. On the pre-#2940 code (always full re-dump
    + estimate) the unchanged history's turns would be estimated 3 times,
    not once."""
    counts: dict[str, int] = {}

    from reyn.services.compaction import engine as engine_mod

    real = engine_mod.estimate_tokens_for_any_turn

    def _counting_estimate_tokens_for_turn(turn, model, *, use_chars4=False):
        key = json.dumps(turn, ensure_ascii=False)
        counts[key] = counts.get(key, 0) + 1
        return real(turn, model, use_chars4=use_chars4)

    monkeypatch.setattr(engine_mod, "estimate_tokens_for_any_turn", _counting_estimate_tokens_for_turn)

    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    advisor = _make_advisor(lambda: history)
    turn0_key = json.dumps(history[0], ensure_ascii=False)
    turn1_key = json.dumps(history[1], ensure_ascii=False)

    advisor.context_window_status()
    assert counts.get(turn0_key) == 1, "first call must estimate each turn once (cold cache)"
    assert counts.get(turn1_key) == 1

    advisor.context_window_status()
    advisor.context_window_status()
    assert counts.get(turn0_key) == 1, (
        "repeated calls with unchanged history must NOT call estimate_tokens_for_any_turn "
        "again for the same turns — this is the O(1) cache-hit path the #2940 fix adds"
    )
    assert counts.get(turn1_key) == 1

    new_turn = {"role": "user", "content": "a new turn"}
    history.append(new_turn)
    advisor.context_window_status()
    new_turn_key = json.dumps(new_turn, ensure_ascii=False)
    assert counts.get(new_turn_key) == 1, (
        "growth must estimate the NEW tail slice's own turn — proves only "
        "the delta was estimated, not the full (now-longer) history again"
    )
    assert counts.get(turn0_key) == 1 and counts.get(turn1_key) == 1, (
        "the unchanged prefix turns must NEVER be re-estimated on growth"
    )


def test_repeated_calls_with_unchanged_history_return_identical_value() -> None:
    """Tier 2: reopening the dropdown with no new turns since the last call
    returns the exact same estimate (cache hit), not a fresh (possibly
    non-deterministic-cost) recompute."""
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    advisor = _make_advisor(lambda: history)

    first = advisor.context_window_status()["free_window"]
    second = advisor.context_window_status()["free_window"]
    third = advisor.context_window_status()["free_window"]
    assert first == second == third


def test_growing_history_matches_from_scratch_computation() -> None:
    """Tier 2: falsifying — the incremental (cache + tail-slice) path must
    EXACTLY match a from-scratch per-turn sum over the final history at every
    growth step. #2957 PR-B: unlike the pre-PR-B combined-json.dumps scheme
    (which could drift at tokenizer merge boundaries when a message was
    estimated jointly vs. separately from its neighbours), per-turn summation
    is associative — each turn's estimate never depends on its neighbours —
    so there is no drift to bound with a tolerance. A real bug (double-
    counted or dropped turn) breaks exact equality immediately."""
    history: list = []
    advisor = _make_advisor(lambda: history)

    for i in range(12):
        history.append({"role": "user", "content": f"message number {i} " * (i + 1)})
        status = advisor.context_window_status()
        used = status["effective_trigger"] - status["free_window"]
        expected = _from_scratch_tokens(history, "openai/gpt-4o")
        assert used == expected, f"diverged at step {i}: {used} vs {expected}"


def test_same_length_but_different_content_recomputes_rather_than_stale(monkeypatch) -> None:
    """Tier 2: falsifying — lead-coder co-vet finding on #2951. history_fn is
    NOT a real append-only array; it's typically Session._active_branch_
    history, a DERIVED view recomputed on every call (the same function
    #2938 hoisted). A rewind can change WHICH messages are active such that
    len(history) returns to a previously-cached value while the content at
    that length is genuinely different (e.g. a message got swapped for a
    longer one after a rewind + new branch). A length-only cache would
    silently keep serving the stale total forever (never re-syncing until a
    LATER length decrease) — this proves the boundary-identity check (the
    hash of the last cached message) catches a same-length content swap."""
    counts: dict[str, int] = {}

    from reyn.services.compaction import engine as engine_mod

    real = engine_mod.estimate_tokens_for_any_turn

    def _counting_estimate_tokens_for_turn(turn, model, *, use_chars4=False):
        key = json.dumps(turn, ensure_ascii=False)
        counts[key] = counts.get(key, 0) + 1
        return real(turn, model, use_chars4=use_chars4)

    monkeypatch.setattr(engine_mod, "estimate_tokens_for_any_turn", _counting_estimate_tokens_for_turn)

    history = [{"role": "user", "content": "original short message"}]
    advisor = _make_advisor(lambda: history)

    first = advisor.context_window_status()["free_window"]

    # Same length (1), but the message at index 0 is now a DIFFERENT,
    # much longer one — simulates a rewind that swapped the active branch's
    # content without changing the active-message count.
    history = [{"role": "user", "content": "a completely different and much longer replacement message " * 20}]
    advisor._history_fn = lambda: history
    second_status = advisor.context_window_status()
    second = second_status["free_window"]

    assert second != first, (
        "a same-length content swap must NOT return the stale cached "
        "free_window from the old (shorter) content"
    )
    used = second_status["effective_trigger"] - second
    expected = _from_scratch_tokens(history, "openai/gpt-4o")
    assert used == expected


def test_shrinking_history_recomputes_rather_than_returning_stale_cache() -> None:
    """Tier 2: falsifying — after compaction/rewind truncates history to
    fewer messages than were cached, the NEXT call must reflect the smaller
    history, not the larger cached total (which would report a bogus
    over-budget / under-budget free_window after every compaction)."""
    history = [{"role": "user", "content": "x" * 500} for _ in range(20)]
    advisor = _make_advisor(lambda: history)

    advisor.context_window_status()  # populate cache at length 20

    history = history[:3]  # simulate compaction truncating the router-view history
    advisor_history_status = advisor.context_window_status()
    used = advisor_history_status["effective_trigger"] - advisor_history_status["free_window"]
    expected = _from_scratch_tokens(history, "openai/gpt-4o")
    assert used == expected


def test_maybe_force_compact_shares_the_same_incremental_cache() -> None:
    """Tier 2: maybe_force_compact's pre-frame history estimate is the SAME
    incremental path as context_window_status (#2940 fix-class: both call
    sites had the identical full-redump pathology) — a call to one primes
    the cache the other then hits.

    Uses a REAL ``CompactionEngine`` (cheaply constructible — literal model
    string, no network) for ``budgets`` rather than a hand-rolled stand-in,
    per testing policy (no inventing fields on a faked dataclass — #3037's
    lesson: this file's own pre-#2957-PR-B revision used a hand-rolled
    ``_FakeBudgets`` missing ``head_budget``/``tail_budget``, which this PR's
    SSoT consolidation (``resolve_effective_trigger_and_budgets`` now reads
    all three) would have silently accepted if left unfixed).
    """
    import asyncio

    from reyn.core.events.events import EventLog
    from reyn.services.compaction.engine import CompactionEngine

    history = [{"role": "user", "content": "hello world"}]
    advisor = _make_advisor(lambda: history)

    engine = CompactionEngine(model="openai/gpt-4o", events=EventLog())

    class _FakeController:
        _engine = engine

        async def force_compact_now(self):
            raise AssertionError("must not be called — history is far under budget")

    advisor._compaction_controller = _FakeController()

    status_before = advisor.context_window_status()
    asyncio.run(advisor.maybe_force_compact())
    status_after = advisor.context_window_status()
    expected_free_window = status_before["effective_trigger"] - _from_scratch_tokens(
        history, "openai/gpt-4o"
    )
    assert status_before["free_window"] == status_after["free_window"] == expected_free_window
