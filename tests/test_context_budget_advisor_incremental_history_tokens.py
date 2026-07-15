"""Tier 2: ContextBudgetAdvisor incremental history-token cache (#2940).

The status-bar ctx chip's dropdown calls context_window_status() every time
it opens, and maybe_force_compact() calls the same estimate every pre-frame
overflow check. Both previously re-ran json.dumps(FULL history) + estimate_
tokens on every single call — O(history size) each time, growing unbounded
over a long session (the reported freeze). _incremental_history_tokens()
caches (history length, cumulative estimate) and only dumps+estimates the
NEW tail slice on growth, returning the cached total unchanged (O(1)) when
history hasn't grown, and fully recomputing (not silently returning stale
data) when history SHRINKS (compaction/rewind truncated it).

Real ContextBudgetAdvisor + real estimate_tokens — no mocks. The estimate
function itself is exact-deterministic for a given text (no LLM call), so
correctness can be checked by comparing the incremental result against a
from-scratch computation over the same final history.
"""
from __future__ import annotations

import json

from reyn.config import CompactionConfig
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.services.compaction.engine import estimate_tokens


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
    combined = json.dumps(history, ensure_ascii=False)
    return estimate_tokens(combined, model, use_chars4=False)


def test_unchanged_history_skips_estimate_entirely(monkeypatch) -> None:
    """Tier 2: falsifying — proves the cache HIT path, not just that its
    output happens to match (a real spy on estimate_tokens, per the #2937
    counting-spy idiom — a real callable recording per-text call counts in a
    dict, not a MagicMock, and not a bare len(list)==N format pin). Reopening
    the dropdown with no new messages since the last call must not
    re-dump/re-estimate at all. On the pre-#2940 code (always full
    json.dumps + estimate_tokens) the unchanged-history text would be
    estimated 3 times, not once."""
    counts: dict[str, int] = {}

    from reyn.services.compaction import engine as engine_mod

    real = engine_mod.estimate_tokens

    def _counting_estimate_tokens(text, model, *, use_chars4=False):
        counts[text] = counts.get(text, 0) + 1
        return real(text, model, use_chars4=use_chars4)

    monkeypatch.setattr(engine_mod, "estimate_tokens", _counting_estimate_tokens)

    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    advisor = _make_advisor(lambda: history)
    unchanged_text = json.dumps(history, ensure_ascii=False)

    advisor.context_window_status()
    assert counts.get(unchanged_text) == 1, "first call must estimate once (cold cache)"

    advisor.context_window_status()
    advisor.context_window_status()
    assert counts.get(unchanged_text) == 1, (
        "repeated calls with unchanged history must NOT call estimate_tokens "
        "again for the same combined text — this is the O(1) cache-hit path "
        "the #2940 fix adds"
    )

    new_turn = {"role": "user", "content": "a new turn"}
    history.append(new_turn)
    advisor.context_window_status()
    delta_text = json.dumps(new_turn, ensure_ascii=False)
    assert counts.get(delta_text) == 1, (
        "growth must estimate the NEW tail slice's own text — proves only "
        "the delta was dumped, not the full (now-longer) history again"
    )
    assert counts.get(json.dumps(history, ensure_ascii=False)) is None, (
        "the full (post-growth) combined history must NEVER be dumped as one "
        "unit — only its individual new tail message is"
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
    track a full from-scratch json.dumps+estimate over the final history at
    every growth step, within a tolerance proportional to message count.

    Exact equality isn't the bound: tokenizing a new message's JSON dump
    SEPARATELY from the rest of the history can shift subword (BPE) merge
    decisions right at the fragment boundary relative to tokenizing the
    whole thing jointly. An earlier revision of this docstring claimed this
    drift is structurally one-directional (incremental >= from-scratch,
    "only ever safe, never under") — DISPROVEN by direct measurement: a
    growing-repeated-phrase history (this test's own fixture) makes
    tokenizing the pieces SEPARATELY (incremental) MORE token-efficient
    (fewer tokens) than tokenizing the whole thing JOINTLY (from-scratch),
    i.e. the incremental estimate UNDER-shoots the from-scratch one by an
    amount that grows with message count — the reverse of "splitting can
    only lose cross-boundary merges, so sum(pieces) >= whole." Verified
    independently of comma/bracket reconstruction — adding the exact
    missing separator punctuation to the delta text does not close the
    gap, so this is a genuine cross-boundary BPE tokenization effect, not a
    punctuation-accounting bug. Realistic multi-turn chat content (varied
    per-message text, not a single repeated phrase) measured EXACTLY zero
    drift in the same harness — the failure mode needs unusually
    repetitive content to manifest — but "always safe" is not something
    this scheme can guarantee, so the tolerance here is an
    empirically-bounded allowance in EITHER direction, not a one-sided
    safety margin. A real bug (double-counted or dropped message) would
    blow far past this bound, which is what this test exists to catch."""
    history: list = []
    advisor = _make_advisor(lambda: history)

    for i in range(12):
        history.append({"role": "user", "content": f"message number {i} " * (i + 1)})
        status = advisor.context_window_status()
        used = status["effective_trigger"] - status["free_window"]
        expected = _from_scratch_tokens(history, "openai/gpt-4o")
        tolerance = 2 * (i + 1)
        assert abs(used - expected) <= tolerance, f"diverged at step {i}: {used} vs {expected}"


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

    real = engine_mod.estimate_tokens

    def _counting_estimate_tokens(text, model, *, use_chars4=False):
        counts[text] = counts.get(text, 0) + 1
        return real(text, model, use_chars4=use_chars4)

    monkeypatch.setattr(engine_mod, "estimate_tokens", _counting_estimate_tokens)

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


def test_maybe_force_compact_shares_the_same_incremental_cache(monkeypatch) -> None:
    """Tier 2: maybe_force_compact's pre-frame history estimate is the SAME
    incremental path as context_window_status (#2940 fix-class: both call
    sites had the identical full-redump pathology) — a call to one primes
    the cache the other then hits."""
    import asyncio

    history = [{"role": "user", "content": "hello world"}]
    advisor = _make_advisor(lambda: history)

    class _FakeBudgets:
        effective_trigger = 10_000_000  # far above any estimate: never force-compacts
        new_msg_budget = 10_000_000

    class _FakeEngine:
        budgets = _FakeBudgets()

        def recompute_budgets(self):
            pass

    class _FakeController:
        _engine = _FakeEngine()

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
