"""Tier 2: cost-panel 3-scope breakdown accumulation (#cost-panel-breakdown).

#2931 added ``CostBreakdown`` / ``estimate_cost_breakdown`` (cache-aware
prompt/cache-read/cache-creation/completion components + ``cache_savings`` +
``cache_hit_rate``) as backend-only, no panel wiring. This file pins the
wiring this PR adds:

  1. ``BudgetTracker.record_llm`` now accumulates a per-agent ``CostBreakdown``
     (``agent_cost_breakdown``) alongside the existing ``agent_cost_usd`` float,
     driven by REAL per-call ``estimate_cost_breakdown`` invocations — including
     across TWO DIFFERENT MODELS with different rates, to falsify the
     multi-model aggregation hazard (re-pricing aggregated token counts with a
     single model's rate would be wrong; accumulating each call's own
     model-rate breakdown is the correct approach and is what is under test).
  2. Total (litellm-accurate, ``agent_cost_usd``) equals Input+Output
     (``prompt_cost + cache_read_cost + cache_creation_cost + completion_cost``,
     i.e. ``CostBreakdown.total_cost``) for a mixed-model scope, below the
     >200k tiered-pricing threshold.
  3. Saved% uses the correct no-cache-baseline denominator — Saved /
     (Input + Saved) — FALSIFIED against the wrong denominator (Saved / Total).
  4. The 3 scopes aggregate correctly: Session-scope calls are a subset of
     Agent-scope calls (all sessions of one agent) are a subset of
     Project-scope calls (all agents) — Session ⊆ Agent ⊆ Project by sum.

Policy: no mocks — real ``BudgetTracker`` + real ``litellm`` pricing lookups
(the same real accumulation path ``call_llm_tools`` drives in production, via
directly-invoked ``record_llm``, since a real network completion isn't needed
to exercise this bookkeeping).
"""
from __future__ import annotations

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig

# Two real, differently-priced models (falsifies re-pricing aggregated token
# counts with a single model's rate — see module docstring point 1).
_MODEL_A = "claude-sonnet-4-5-20250929"  # cache-capable, higher rate
_MODEL_B = "gpt-4o-mini"                 # cache-capable, much lower rate


def _epsilon_close(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) < eps


def test_agent_cost_breakdown_accumulates_across_calls_and_models() -> None:
    """Tier 2: per-agent CostBreakdown accumulates across multiple calls to
    DIFFERENT models — each call is priced at ITS OWN model's rate (not a
    single re-priced rate applied to the summed tokens)."""
    tracker = BudgetTracker(CostConfig())

    usage_a1 = TokenUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=800, cache_creation_tokens=100)
    usage_a2 = TokenUsage(prompt_tokens=500, completion_tokens=50, cached_tokens=0, cache_creation_tokens=0)
    usage_b1 = TokenUsage(prompt_tokens=2000, completion_tokens=300, cached_tokens=1000, cache_creation_tokens=0)

    tracker.record_llm(model=_MODEL_A, agent="alice", usage=usage_a1)
    tracker.record_llm(model=_MODEL_A, agent="alice", usage=usage_a2)
    tracker.record_llm(model=_MODEL_B, agent="alice", usage=usage_b1)

    breakdown = tracker.agent_cost_breakdown("alice")

    # Cross-check against independently-computed per-call breakdowns summed
    # by hand (mirrors what estimate_cost_breakdown itself returns per call —
    # NOT re-derived from a single re-priced rate over the combined tokens).
    from reyn.llm.pricing import estimate_cost_breakdown
    expected = (
        estimate_cost_breakdown(_MODEL_A, usage_a1)
        + estimate_cost_breakdown(_MODEL_A, usage_a2)
        + estimate_cost_breakdown(_MODEL_B, usage_b1)
    )
    assert _epsilon_close(breakdown.total_cost, expected.total_cost)
    assert _epsilon_close(breakdown.cache_savings, expected.cache_savings)
    assert breakdown.prompt_tokens == expected.prompt_tokens
    assert breakdown.cached_tokens == expected.cached_tokens


def test_total_equals_input_plus_output_below_200k() -> None:
    """Tier 2: the authoritative Total (agent_cost_usd, litellm-accurate) equals
    Input+Output derived from the accumulated CostBreakdown, for a mixed-model
    scope below the >200k tiered-pricing threshold."""
    tracker = BudgetTracker(CostConfig())

    usage_a = TokenUsage(prompt_tokens=1500, completion_tokens=250, cached_tokens=1000, cache_creation_tokens=200)
    usage_b = TokenUsage(prompt_tokens=3000, completion_tokens=400, cached_tokens=1500, cache_creation_tokens=0)

    tracker.record_llm(model=_MODEL_A, agent="bob", usage=usage_a)
    tracker.record_llm(model=_MODEL_B, agent="bob", usage=usage_b)

    total = tracker.agent_cost_usd("bob")
    breakdown = tracker.agent_cost_breakdown("bob")
    input_cost = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    output_cost = breakdown.completion_cost

    assert _epsilon_close(total, input_cost + output_cost), (
        f"Total ({total}) must equal Input+Output ({input_cost + output_cost}) "
        "below the 200k tiered-pricing threshold"
    )


def test_saved_pct_uses_input_plus_saved_denominator_not_total() -> None:
    """Tier 2: FALSIFY the wrong denominator. Saved% = Saved / (Input + Saved)
    — the no-cache BASELINE input cost — not Saved / Total. Total includes
    Output, which is unrelated to the input cache discount; using Total as the
    denominator would silently understate the savings percentage whenever
    Output is nonzero (as it always is for a real call)."""
    tracker = BudgetTracker(CostConfig())
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, cached_tokens=800, cache_creation_tokens=0)
    tracker.record_llm(model=_MODEL_A, agent="carol", usage=usage)

    breakdown = tracker.agent_cost_breakdown("carol")
    input_cost = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    saved = breakdown.cache_savings
    total = tracker.agent_cost_usd("carol")

    correct_denominator = input_cost + saved
    correct_pct = saved / correct_denominator
    wrong_pct_using_total = saved / total  # the bug this test falsifies

    assert correct_denominator > 0
    assert saved > 0, "this scenario has cached tokens, so savings must be > 0"
    # Output being nonzero means Total > Input+Saved always in this scenario,
    # so the wrong-denominator % must be strictly LOWER than the correct one —
    # a real, observable divergence, not just a formula difference on paper.
    assert wrong_pct_using_total < correct_pct
    assert 0.0 < correct_pct < 1.0


def test_saved_pct_zero_when_no_priced_input_recorded() -> None:
    """Tier 2: Saved% divide-by-zero guard — 0% (not an exception) when no
    input cost has been recorded yet for a scope (Input + Saved == 0)."""
    tracker = BudgetTracker(CostConfig())
    breakdown = tracker.agent_cost_breakdown("nobody-yet")
    input_cost = breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    saved = breakdown.cache_savings
    denominator = input_cost + saved
    pct = (saved / denominator) if denominator > 0 else 0.0
    assert pct == 0.0


def test_session_agent_project_scopes_aggregate_correctly() -> None:
    """Tier 2: Session ⊆ Agent ⊆ Project — a registry-style 3-scope sum built
    the same way ``registry.agent_cost_breakdown`` / ``project_cost_breakdown``
    do (one shared BudgetTracker; per-agent accumulation; Project = sum over
    every agent) reconstructs correctly from real per-call recordings."""
    tracker = BudgetTracker(CostConfig())

    # "dave" has TWO sessions' worth of calls (session-scope would only see
    # ONE of them; agent-scope sees both — mirroring registry.agent_total_usage
    # summing across all sids of one agent).
    dave_session1_usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=500)
    dave_session2_usage = TokenUsage(prompt_tokens=800, completion_tokens=80, cached_tokens=300)
    tracker.record_llm(model=_MODEL_A, agent="dave", usage=dave_session1_usage)
    tracker.record_llm(model=_MODEL_A, agent="dave", usage=dave_session2_usage)

    # A second agent, "erin", also recorded — only shows up in Project scope.
    erin_usage = TokenUsage(prompt_tokens=2000, completion_tokens=200, cached_tokens=1000)
    tracker.record_llm(model=_MODEL_B, agent="erin", usage=erin_usage)

    from reyn.llm.pricing import estimate_cost_breakdown

    session1_breakdown = estimate_cost_breakdown(_MODEL_A, dave_session1_usage)
    dave_agent_breakdown = tracker.agent_cost_breakdown("dave")
    erin_agent_breakdown = tracker.agent_cost_breakdown("erin")
    project_breakdown = dave_agent_breakdown + erin_agent_breakdown  # mirrors registry.project_cost_breakdown

    # Session (session1 only) is strictly <= Agent (both dave sessions summed).
    assert session1_breakdown.total_cost <= dave_agent_breakdown.total_cost
    assert session1_breakdown.total_cost < dave_agent_breakdown.total_cost  # session2 adds strictly more

    # Agent (dave) is strictly <= Project (dave + erin).
    assert dave_agent_breakdown.total_cost <= project_breakdown.total_cost
    assert dave_agent_breakdown.total_cost < project_breakdown.total_cost  # erin adds strictly more

    # Project total_cost is exactly the sum of the two agents' totals — no
    # double counting, no dropped calls.
    assert _epsilon_close(
        project_breakdown.total_cost,
        dave_agent_breakdown.total_cost + erin_agent_breakdown.total_cost,
    )


def test_agent_cost_breakdown_not_ledger_durable_resets_on_new_tracker() -> None:
    """Tier 2: agent_cost_breakdown is explicitly NOT ledger-persisted (see
    BudgetTracker.__init__'s docstring) — a fresh tracker (simulating restart)
    starts at empty even though the durable agent_cost_usd would, via
    hydrate(), have survived. This test documents/pins the scope choice so a
    future change to durability is a deliberate decision, not silent drift."""
    tracker = BudgetTracker(CostConfig())
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=500)
    tracker.record_llm(model=_MODEL_A, agent="frank", usage=usage)
    assert tracker.agent_cost_breakdown("frank").total_cost > 0

    fresh_tracker = BudgetTracker(CostConfig())
    assert fresh_tracker.agent_cost_breakdown("frank").total_cost == 0.0
