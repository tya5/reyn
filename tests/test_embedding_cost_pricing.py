"""Tier 2: FP-0063 PC — embedding cost pricing (`estimate_embedding_cost` /
`EmbeddingCost`), independent of the chat `CostBreakdown`.

Proposal 0063 ("Embedding cost tracking") verified that `pricing.py` resolved
completion rates ONLY — no embedding-mode rate was ever looked up, so
embedding spend never reached a dollar figure. This file pins the extension:

  1. `estimate_embedding_cost` resolves a REAL litellm embedding-mode model
     (no new/parallel rate table — same `litellm.model_cost` lookup
     `estimate_cost` uses for chat, extended to embedding entries).
  2. An unknown/unpriced model returns `(None, None)` — VISIBLE unknown, never
     a silent `$0.00` (mirrors the pre-existing #1829 sentinel for chat).
  3. `EmbeddingCost` aggregates ADDITIVELY across calls — and the headline
     requirement (X6): two calls at DIFFERENT models' DIFFERENT rates sum to
     the SAME total as pricing each independently and adding the dollars.
     This test also FALSIFIES the wrong approach (summing tokens across
     models, then pricing once at a single model's rate) — the module
     docstring / PR body report the falsification result.

Policy: no mocks — real litellm + real `model_cost` lookups; no
private-state assertions; Tier line.
"""
from __future__ import annotations

from reyn.llm.pricing import EmbeddingCost, estimate_embedding_cost

# Two real, differently-priced embedding-mode models (litellm.model_cost,
# mode="embedding") — verified present with distinct `input_cost_per_token`
# rates in the vendored litellm at authoring time.
_MODEL_A = "text-embedding-3-small"   # OpenAI, cheaper
_MODEL_B = "text-embedding-3-large"   # OpenAI, pricier


def _epsilon_close(a: float, b: float, eps: float = 1e-12) -> bool:
    return abs(a - b) < eps


def test_estimate_embedding_cost_resolves_a_real_litellm_model() -> None:
    """Tier 2: a real embedding-mode model resolves a positive dollar cost —
    litellm's embedding-mode rate table is reachable (extends the existing
    lookup; not a new rate table)."""
    cost_usd, snapshot = estimate_embedding_cost(_MODEL_A, 1000)
    assert cost_usd is not None
    assert cost_usd > 0.0
    assert snapshot is not None
    assert snapshot["model"] == _MODEL_A
    assert snapshot["source"] == "litellm"
    assert snapshot["input_per_1m_usd"] > 0


def test_estimate_embedding_cost_two_models_have_different_rates() -> None:
    """Tier 2: precondition for the mixed-model test below — the two chosen
    models must actually price differently, or the falsification downstream
    would be vacuous."""
    cost_a, _ = estimate_embedding_cost(_MODEL_A, 1_000_000)
    cost_b, _ = estimate_embedding_cost(_MODEL_B, 1_000_000)
    assert cost_a is not None and cost_b is not None
    assert cost_a != cost_b


def test_estimate_embedding_cost_unknown_model_is_none_not_zero() -> None:
    """Tier 2: an unpriced/unknown model returns (None, None) — unknown != free
    (#1829 sentinel extended to embedding mode). A silent $0.00 here would
    recreate the exact invisibility bug FP-0063 exists to close."""
    cost_usd, snapshot = estimate_embedding_cost("not-a-real-model-xyz", 1000)
    assert cost_usd is None
    assert snapshot is None


def test_estimate_embedding_cost_zero_tokens_is_free_but_priced() -> None:
    """Tier 2: zero tokens costs $0.00 (nothing to price) — distinct from the
    unknown-model case, which is None specifically because the RATE, not the
    tokens, is missing."""
    cost_usd, snapshot = estimate_embedding_cost(_MODEL_A, 0)
    assert cost_usd == 0.0
    assert snapshot is None


def test_embedding_cost_additive_across_calls() -> None:
    """Tier 2: `EmbeddingCost.__add__` / `__iadd__` sum all four fields."""
    a = EmbeddingCost(cost_usd=1.0, tokens=100, calls=1, unpriced_calls=0)
    b = EmbeddingCost(cost_usd=2.0, tokens=200, calls=1, unpriced_calls=1)
    total = a + b
    assert total.cost_usd == 3.0
    assert total.tokens == 300
    assert total.calls == 2
    assert total.unpriced_calls == 1

    a += b
    assert a.cost_usd == 3.0
    assert a.tokens == 300
    assert a.calls == 2
    assert a.unpriced_calls == 1


def test_mixed_model_correctness_prices_each_call_at_its_own_rate() -> None:
    """Tier 2 (X6 headline test): two embedding calls at DIFFERENT models with
    DIFFERENT rates must aggregate to the SAME total as pricing each
    independently at its own rate and summing the dollars.

    FALSIFICATION (reported in the PR body): the WRONG approach — sum the
    token counts across both calls, then price ONCE at a single model's rate
    — is asserted to differ from the correct total whenever the two models'
    rates differ (guaranteed by the precondition test above). This is the
    failure mode X6 exists to prevent (aggregating tokens across models before
    pricing, instead of pricing per-call then aggregating dollars).
    """
    tokens_a = 10_000
    tokens_b = 25_000

    cost_a, _ = estimate_embedding_cost(_MODEL_A, tokens_a)
    cost_b, _ = estimate_embedding_cost(_MODEL_B, tokens_b)
    assert cost_a is not None and cost_b is not None

    # Correct: price each call at its own model's rate, aggregate dollars.
    aggregate = EmbeddingCost(cost_usd=cost_a, tokens=tokens_a, calls=1) + EmbeddingCost(
        cost_usd=cost_b, tokens=tokens_b, calls=1,
    )
    correct_total = aggregate.cost_usd
    assert _epsilon_close(correct_total, cost_a + cost_b)

    # WRONG (falsified): pool tokens across models, price once at model A's
    # rate as if the whole batch used a single session-level model.
    wrong_total, _ = estimate_embedding_cost(_MODEL_A, tokens_a + tokens_b)
    assert wrong_total is not None

    assert not _epsilon_close(wrong_total, correct_total), (
        "the wrong (pool-then-price-once) approach must diverge from the "
        "correct (price-per-call-then-sum) approach whenever the two models' "
        "rates differ — if they matched, this test would not be falsifying "
        "anything"
    )
