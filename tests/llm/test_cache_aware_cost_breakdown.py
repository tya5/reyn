"""Tier 2: cache-aware cost total + 4-component breakdown (#cache-cost-accuracy).

reyn's cost accounting was cache-UNAWARE: ``estimate_cost`` called
``litellm.cost_per_token(prompt_tokens=..., completion_tokens=...)`` without
the cache breakdown, so cached (cache-read) prompt tokens were billed at the
full input rate instead of the discounted cache-read rate. reyn already
CAPTURED the cache tokens (``TokenUsage.cached_tokens`` / ``.cache_creation_
tokens``, #1772) — they were just not used in the cost formula.

This file pins:
  1. ``estimate_cost`` now returns the cache-aware total (decisively lower
     than the old naive total on a cached scenario — FALSIFYING the old
     overcharge).
  2. ``estimate_cost_breakdown`` returns the 4 components (prompt / cache-read
     / cache-creation / completion) that SUM to the same cache-aware total
     litellm itself computes (the components-sum-to-total invariant).
  3. An unpriced/unknown model still returns ``None`` (no #1829 regression),
     for both ``estimate_cost`` and ``estimate_cost_breakdown``.

Provider-convention note pinned by these tests: ``TokenUsage.prompt_tokens``
ALREADY INCLUDES ``cached_tokens`` + ``cache_creation_tokens`` as subsets
(litellm's own Anthropic-transformation adds both cache figures into
``response.usage.prompt_tokens`` before reyn ever sees it) — so the
non-cached ("regular") portion priced at the full input rate is
``prompt_tokens - cached_tokens - cache_creation_tokens``, not
``prompt_tokens`` itself. See ``pricing.py``'s ``_usage_object_for`` /
``estimate_cost_breakdown`` docstrings for the empirical verification this
relies on (a real ``litellm.cost_per_token`` call, no mocks).

Policy: no mocks — real litellm + real model_cost lookups; no private-state
assertions; Tier line.
"""
from __future__ import annotations

from reyn.llm.pricing import (
    CostBreakdown,
    TokenUsage,
    estimate_cost,
    estimate_cost_breakdown,
)

# A real cache-capable Anthropic model (supports_prompt_caching=True, has
# cache_read_input_token_cost / cache_creation_input_token_cost entries in
# litellm.model_cost as of the pinned litellm version this repo vendors).
_CACHE_MODEL = "claude-sonnet-4-5-20250929"


def _epsilon_close(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) < eps


def test_cache_aware_total_prices_cached_tokens_at_cache_rate() -> None:
    """Tier 2: cached tokens are billed at the cache-read rate, not the full
    input rate — decisively lower total than the old (naive) formula.

    FALSIFY the old path: the old formula was
    ``litellm.cost_per_token(model=model, prompt_tokens=P, completion_tokens=C)``
    with NO cache args — which bills the full 1000 prompt tokens (including
    the 800 cached + 100 cache-creation) at the plain input rate. That naive
    total must be strictly GREATER than the new cache-aware total.
    """
    usage = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=800,
        cache_creation_tokens=100,
    )

    cache_aware_cost, snapshot = estimate_cost(_CACHE_MODEL, usage)
    assert cache_aware_cost is not None and snapshot is not None

    import litellm

    naive_prompt_cost, naive_completion_cost = litellm.cost_per_token(
        model=_CACHE_MODEL,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
    )
    naive_total = naive_prompt_cost + naive_completion_cost

    assert cache_aware_cost < naive_total, (
        "cache-aware total must be strictly less than the naive (cache-unaware) "
        "total — otherwise the cache-read discount is not being applied"
    )
    delta = naive_total - cache_aware_cost
    assert delta > 0.001, (
        f"expected a material overcharge delta on this cached scenario, got {delta}"
    )


def test_breakdown_components_sum_to_cache_aware_total() -> None:
    """Tier 2: the 4 breakdown components SUM to litellm's cache-aware total
    (the decisive components-sum-to-total invariant)."""
    usage = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=800,
        cache_creation_tokens=100,
    )

    cache_aware_cost, _ = estimate_cost(_CACHE_MODEL, usage)
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)

    assert breakdown is not None
    assert cache_aware_cost is not None
    assert _epsilon_close(breakdown.total_cost, cache_aware_cost), (
        f"components sum ({breakdown.total_cost}) must equal litellm's "
        f"cache-aware total ({cache_aware_cost})"
    )


def test_breakdown_cache_read_uses_discounted_rate() -> None:
    """Tier 2: cache_read_cost is priced at ~10% of the input rate for this
    model (Anthropic's published cache-read discount), not the full input rate."""
    usage = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=800,
        cache_creation_tokens=100,
    )
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)
    assert breakdown is not None

    import litellm
    entry = litellm.model_cost[_CACHE_MODEL]
    input_rate = entry["input_cost_per_token"]
    cache_read_rate = entry["cache_read_input_token_cost"]

    # cache-read rate must be a material discount vs the full input rate
    assert cache_read_rate < input_rate * 0.5
    # and the computed cache_read_cost must reflect the discounted rate,
    # not the full input rate, for the 800 cached tokens
    full_rate_cost = 800 * input_rate
    assert breakdown.cache_read_cost < full_rate_cost


def test_breakdown_regular_prompt_excludes_cache_subsets() -> None:
    """Tier 2: the non-cached ("regular") prompt portion is
    prompt_tokens - cached_tokens - cache_creation_tokens (1000-800-100=100),
    NOT the full prompt_tokens (1000) — pins the provider-convention
    assumption documented in ``estimate_cost_breakdown``."""
    usage = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=800,
        cache_creation_tokens=100,
    )
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)
    assert breakdown is not None

    import litellm
    input_rate = litellm.model_cost[_CACHE_MODEL]["input_cost_per_token"]
    expected_regular_prompt_cost = 100 * input_rate  # 1000 - 800 - 100 = 100
    assert _epsilon_close(breakdown.prompt_cost, expected_regular_prompt_cost)


def test_breakdown_aggregates_across_turns() -> None:
    """Tier 2: CostBreakdown.__add__ / __iadd__ sum components across turns,
    mirroring TokenUsage's own aggregation contract."""
    usage_a = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=800, cache_creation_tokens=100)
    usage_b = TokenUsage(prompt_tokens=500, completion_tokens=100, cached_tokens=400, cache_creation_tokens=0)

    b_a = estimate_cost_breakdown(_CACHE_MODEL, usage_a)
    b_b = estimate_cost_breakdown(_CACHE_MODEL, usage_b)
    assert b_a is not None and b_b is not None

    combined = b_a + b_b
    assert _epsilon_close(combined.prompt_cost, b_a.prompt_cost + b_b.prompt_cost)
    assert _epsilon_close(combined.total_cost, b_a.total_cost + b_b.total_cost)

    running = CostBreakdown()
    running += b_a
    running += b_b
    assert _epsilon_close(running.total_cost, combined.total_cost)


def test_cache_savings_equals_fullprice_minus_actual_cache_read() -> None:
    """Tier 2: cache_savings = (full input-rate cost of the cached tokens) −
    (actual cache-read cost of the cached tokens). This is the money the
    cache-read discount saved on this call."""
    usage = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=800,
        cache_creation_tokens=100,
    )
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)
    assert breakdown is not None

    import litellm
    entry = litellm.model_cost[_CACHE_MODEL]
    input_rate = entry["input_cost_per_token"]
    cache_read_rate = entry["cache_read_input_token_cost"]

    full_price_of_cached = 800 * input_rate
    actual_cache_read_cost = 800 * cache_read_rate
    expected_savings = full_price_of_cached - actual_cache_read_cost

    assert _epsilon_close(breakdown.cache_savings, expected_savings)
    # sanity: savings == full-price-of-cached − the breakdown's own cache_read_cost
    assert _epsilon_close(
        breakdown.cache_savings, full_price_of_cached - breakdown.cache_read_cost
    )
    assert breakdown.cache_savings > 0


def test_cache_hit_rate_and_divide_by_zero_guard() -> None:
    """Tier 2: cache_hit_rate = cached_tokens / prompt_tokens; 0.0 when
    prompt_tokens is 0 (divide-by-zero guard)."""
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=800)
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)
    assert breakdown is not None
    assert _epsilon_close(breakdown.cache_hit_rate, 0.8)

    # zero-usage → empty breakdown → guarded 0.0, not ZeroDivisionError
    empty = estimate_cost_breakdown(_CACHE_MODEL, TokenUsage())
    assert empty is not None
    assert empty.cache_hit_rate == 0.0


def test_savings_and_hit_rate_aggregate_across_turns() -> None:
    """Tier 2: cache_savings sums additively; cache_hit_rate is recomputed
    from the AGGREGATED token counts (not a sum of per-turn ratios)."""
    usage_a = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=800)
    usage_b = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=200)

    b_a = estimate_cost_breakdown(_CACHE_MODEL, usage_a)
    b_b = estimate_cost_breakdown(_CACHE_MODEL, usage_b)
    assert b_a is not None and b_b is not None

    combined = b_a + b_b
    assert _epsilon_close(combined.cache_savings, b_a.cache_savings + b_b.cache_savings)
    # aggregated hit rate = (800 + 200) / (1000 + 1000) = 0.5, NOT (0.8 + 0.2)
    assert _epsilon_close(combined.cache_hit_rate, 0.5)


def test_unpriced_model_returns_none_not_zero_for_total_and_breakdown() -> None:
    """Tier 2: an unpriced/unknown model → None (unknown != free, #1829 —
    both the total AND the breakdown must preserve this sentinel)."""
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=100)
    unknown_model = "this-model-does-not-exist-in-litellm-pricing-db-xyz"

    cost, snapshot = estimate_cost(unknown_model, usage)
    assert cost is None and snapshot is None

    breakdown = estimate_cost_breakdown(unknown_model, usage)
    assert breakdown is None


def test_status_bar_total_equals_sum_of_breakdown() -> None:
    """Tier 2: the value the status bar would show (estimate_cost's total)
    equals the sum of the panel's future component breakdown — the two
    surfaces must never visibly disagree."""
    usage = TokenUsage(
        prompt_tokens=2000,
        completion_tokens=300,
        cached_tokens=1500,
        cache_creation_tokens=50,
    )
    total, _ = estimate_cost(_CACHE_MODEL, usage)
    breakdown = estimate_cost_breakdown(_CACHE_MODEL, usage)
    assert total is not None and breakdown is not None
    assert _epsilon_close(total, breakdown.total_cost)
