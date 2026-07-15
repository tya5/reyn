"""Tier 2: FP-0063 PC — embedding cost is tracked as its OWN independent
aggregate, at session / agent / project scope, and does NOT touch the chat
`CostBreakdown`.

Owner decision (2026-07-15, proposal 0063 "Embedding cost is tracked
INDEPENDENTLY"): embedding spend is its own tracked aggregate, never folded
into `CostBreakdown` (embedding is input-only / structurally uncacheable;
mapping it onto `prompt_cost` would dilute `cache_hit_rate` / `cache_savings`,
chat-call-only figures). This file pins:

  1. `BudgetTracker.record_embedding` accumulates a per-agent `EmbeddingCost`
     — INDEPENDENT of `agent_cost_usd` / `agent_cost_breakdown` (the chat
     aggregates), which this test asserts stay untouched by embedding calls
     (the regression this design exists to prevent).
  2. Mixed-model correctness (X6) at the tracker level: two embedding calls
     to DIFFERENT models accumulate to the sum of each priced at its own
     rate — FALSIFIED against pooling tokens and pricing once.
  3. `Registry.agent_embedding_cost` / `.project_embedding_cost` mirror the
     existing `agent_cost_breakdown` / `project_cost_breakdown` per-scope
     shape (Session ⊆ Agent ⊆ Project), applied to the separate aggregate.
  4. `BudgetGateway.record_embedding` / `.embedding_cost` — the Session-scope
     reader — also independent of `total_cost_breakdown`.
  5. An unpriced/unknown model is visible (`unpriced_calls` increments)
     rather than silently reading as free.

Policy: no mocks — real `BudgetTracker` / `BudgetGateway` / `AgentRegistry` +
real `Session`, real litellm pricing lookups; no private-state assertions
(all reads go through the public `agent_cost_usd` / `agent_cost_breakdown` /
`agent_embedding_cost` / `snapshot()`-style accessors); Tier line.
"""
from __future__ import annotations

from reyn.llm.pricing import EmbeddingCost, TokenUsage, estimate_embedding_cost
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.budget_gateway import BudgetGateway
from reyn.core.events.events import EventLog

_MODEL_A = "text-embedding-3-small"
_MODEL_B = "text-embedding-3-large"
_CHAT_MODEL = "claude-sonnet-4-5-20250929"


def _epsilon_close(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) < eps


# ---------------------------------------------------------------------------
# BudgetTracker: agent-scope independent aggregate
# ---------------------------------------------------------------------------


def test_record_embedding_accumulates_per_agent() -> None:
    """Tier 2: repeated embedding calls for one agent accumulate tokens/cost/
    calls in the independent aggregate."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_embedding(model=_MODEL_A, agent="alice", tokens=1000)
    tracker.record_embedding(model=_MODEL_A, agent="alice", tokens=500)

    agg = tracker.agent_embedding_cost("alice")
    assert agg.tokens == 1500
    assert agg.calls == 2
    assert agg.unpriced_calls == 0
    assert agg.cost_usd > 0.0


def test_record_embedding_mixed_models_sums_each_at_its_own_rate() -> None:
    """Tier 2 (X6): two calls to DIFFERENT models accumulate to the sum of
    each priced independently — FALSIFIED against pooling the tokens and
    pricing once at a single model's rate."""
    tracker = BudgetTracker(CostConfig())
    tokens_a, tokens_b = 10_000, 25_000
    tracker.record_embedding(model=_MODEL_A, agent="bob", tokens=tokens_a)
    tracker.record_embedding(model=_MODEL_B, agent="bob", tokens=tokens_b)

    agg = tracker.agent_embedding_cost("bob")
    cost_a, _ = estimate_embedding_cost(_MODEL_A, tokens_a)
    cost_b, _ = estimate_embedding_cost(_MODEL_B, tokens_b)
    correct_total = cost_a + cost_b

    assert _epsilon_close(agg.cost_usd, correct_total)

    # Falsify: pooling the tokens and pricing once at model A's rate must
    # diverge (the two models have different rates — pinned in
    # test_embedding_cost_pricing.py).
    wrong_total, _ = estimate_embedding_cost(_MODEL_A, tokens_a + tokens_b)
    assert not _epsilon_close(agg.cost_usd, wrong_total)


def test_record_embedding_does_not_touch_chat_cost_breakdown() -> None:
    """Tier 2: the regression this design exists to prevent — recording
    embedding calls leaves the chat `agent_cost_usd` / `agent_cost_breakdown`
    at exactly zero (they are driven only by `record_llm`)."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_embedding(model=_MODEL_A, agent="carol", tokens=100_000)

    assert tracker.agent_embedding_cost("carol").cost_usd > 0.0
    assert tracker.agent_cost_usd("carol") == 0.0
    breakdown = tracker.agent_cost_breakdown("carol")
    assert breakdown.total_cost == 0.0
    assert breakdown.prompt_tokens == 0
    assert breakdown.cached_tokens == 0


def test_chat_cost_breakdown_savings_math_unaffected_by_embedding_activity() -> None:
    """Tier 2: interleaving embedding calls with a real chat `record_llm` call
    leaves the chat breakdown's savings math (components-sum-to-total,
    cache_hit_rate) byte-identical to a scope with NO embedding activity —
    embedding tokens never leak into the cache_hit_rate denominator."""
    tracker_with_embeddings = BudgetTracker(CostConfig())
    tracker_without = BudgetTracker(CostConfig())

    usage = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=800)
    tracker_with_embeddings.record_embedding(model=_MODEL_A, agent="dave", tokens=50_000)
    tracker_with_embeddings.record_llm(model=_CHAT_MODEL, agent="dave", usage=usage)
    tracker_with_embeddings.record_embedding(model=_MODEL_B, agent="dave", tokens=20_000)

    tracker_without.record_llm(model=_CHAT_MODEL, agent="dave", usage=usage)

    with_bd = tracker_with_embeddings.agent_cost_breakdown("dave")
    without_bd = tracker_without.agent_cost_breakdown("dave")

    assert _epsilon_close(with_bd.total_cost, without_bd.total_cost)
    assert _epsilon_close(with_bd.cache_hit_rate, without_bd.cache_hit_rate)
    assert _epsilon_close(with_bd.cache_savings, without_bd.cache_savings)
    assert with_bd.prompt_tokens == without_bd.prompt_tokens
    assert with_bd.cached_tokens == without_bd.cached_tokens
    assert _epsilon_close(
        tracker_with_embeddings.agent_cost_usd("dave"),
        tracker_without.agent_cost_usd("dave"),
    )


def test_record_embedding_unpriced_model_is_visible_not_silent_zero() -> None:
    """Tier 2: an unknown/unpriced model still counts toward tokens/calls but
    contributes 0 to cost_usd, with unpriced_calls incremented — the spend
    gap stays observable rather than silently reading as a real $0.00 call."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_embedding(model="not-a-real-model-xyz", agent="erin", tokens=5000)

    agg = tracker.agent_embedding_cost("erin")
    assert agg.tokens == 5000
    assert agg.calls == 1
    assert agg.unpriced_calls == 1
    assert agg.cost_usd == 0.0


def test_record_embedding_with_no_agent_is_a_noop() -> None:
    """Tier 2: agent=None (no attribution target) records nothing — mirrors
    `record_llm`'s agent-gated accumulation."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_embedding(model=_MODEL_A, agent=None, tokens=1000)
    assert tracker.agent_embedding_cost("").tokens == 0


def test_reset_all_clears_embedding_aggregate() -> None:
    """Tier 2: `/budget reset` (`reset_all`) clears the embedding aggregate
    alongside the existing per-agent counters."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_embedding(model=_MODEL_A, agent="frank", tokens=1000)
    assert tracker.agent_embedding_cost("frank").calls == 1

    tracker.reset_all()
    assert tracker.agent_embedding_cost("frank").calls == 0
    assert tracker.agent_embedding_cost("frank").cost_usd == 0.0


# ---------------------------------------------------------------------------
# Registry: 3-scope aggregation (agent / project) mirrors agent_cost_breakdown
# ---------------------------------------------------------------------------


def _registry(tmp_path, tracker: "BudgetTracker | None" = None) -> AgentRegistry:
    from reyn.runtime.session import Session

    shared = tracker if tracker is not None else BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def test_registry_agent_and_project_embedding_cost(tmp_path) -> None:
    """Tier 2: `Registry.agent_embedding_cost` reads the shared tracker's
    per-agent aggregate; `project_embedding_cost` sums every loaded agent —
    Agent <= Project, strictly less when a second agent has activity (mirrors
    `test_session_agent_project_scopes_aggregate_correctly` for the chat
    breakdown, applied to the independent embedding aggregate)."""
    tracker = BudgetTracker(CostConfig())
    reg = _registry(tmp_path, tracker=tracker)

    reg.create("gabe")
    reg.create("hana")
    reg.get_or_load("gabe")
    reg.get_or_load("hana")

    tracker.record_embedding(model=_MODEL_A, agent="gabe", tokens=10_000)
    tracker.record_embedding(model=_MODEL_B, agent="hana", tokens=20_000)

    gabe_cost = reg.agent_embedding_cost("gabe")
    hana_cost = reg.agent_embedding_cost("hana")
    project_cost = reg.project_embedding_cost()

    assert gabe_cost.cost_usd > 0.0
    assert hana_cost.cost_usd > 0.0
    assert _epsilon_close(project_cost.cost_usd, gabe_cost.cost_usd + hana_cost.cost_usd)
    assert gabe_cost.cost_usd < project_cost.cost_usd
    assert project_cost.tokens == gabe_cost.tokens + hana_cost.tokens


def test_registry_agent_embedding_cost_empty_when_no_tracker(tmp_path) -> None:
    """Tier 2: no process-shared tracker wired (no session loaded yet) ->
    empty EmbeddingCost, not an error."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=lambda p: None)
    assert reg.agent_embedding_cost("nobody") == EmbeddingCost()
    assert reg.project_embedding_cost() == EmbeddingCost()


# ---------------------------------------------------------------------------
# BudgetGateway: session-scope independent aggregate
# ---------------------------------------------------------------------------


def test_budget_gateway_records_session_scope_embedding_cost() -> None:
    """Tier 2: `BudgetGateway.record_embedding` accumulates the session-scope
    aggregate, independent of `total_cost_breakdown`."""
    gateway = BudgetGateway(budget_tracker=None, events=EventLog(), agent_name="iris")

    gateway.record_embedding(model=_MODEL_A, tokens=10_000)
    gateway.record_embedding(model=_MODEL_B, tokens=5_000)

    agg = gateway.embedding_cost
    cost_a, _ = estimate_embedding_cost(_MODEL_A, 10_000)
    cost_b, _ = estimate_embedding_cost(_MODEL_B, 5_000)
    assert _epsilon_close(agg.cost_usd, cost_a + cost_b)
    assert agg.tokens == 15_000
    assert agg.calls == 2

    # Untouched by embedding activity.
    assert gateway.total_cost_usd == 0.0
    assert gateway.total_cost_breakdown.total_cost == 0.0
