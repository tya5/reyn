"""Tier 2: ContextBudgetAdvisor.raw_context_window (status-bar ctx chip).

The chip's headline "how close to the model's hard limit" figure must compare
the last request's REAL prompt_tokens against the model's REAL context window
(get_max_input_tokens) — NOT against ``context_window_status()``'s
``effective_trigger``, which is already reduced by SP/head/tail/component-
weight budgeting for compaction-trigger purposes and is a materially smaller
number. Real ContextBudgetAdvisor instances, no mocks.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.llm.model_budget import get_max_input_tokens, get_max_input_tokens_source
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor


def _make_advisor(*, model: str, compaction_controller=None) -> ContextBudgetAdvisor:
    return ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=compaction_controller,
        media_store=None,
        model_fn=lambda: model,
        events=None,
        history_fn=lambda: [],
    )


def test_raw_context_window_returns_the_models_real_window() -> None:
    """Tier 2: raw_context_window()["window"] equals get_max_input_tokens(model)
    directly — no compaction-engine adjustment, unlike effective_trigger."""
    advisor = _make_advisor(model="openai/gpt-4o")
    result = advisor.raw_context_window()
    assert result["window"] == get_max_input_tokens("openai/gpt-4o")


def test_raw_context_window_source_matches_model_budget_source() -> None:
    """Tier 2: the source string is exactly get_max_input_tokens_source(model)
    (litellm catalog vs reyn fallback), not a compaction-engine description."""
    advisor = _make_advisor(model="openai/gpt-4o")
    result = advisor.raw_context_window()
    assert result["source"] == get_max_input_tokens_source("openai/gpt-4o")


def test_raw_context_window_differs_from_effective_trigger_with_engine_budgets() -> None:
    """Tier 2: falsification of the original bug — with a real compaction
    engine attached, effective_trigger is SP/head/tail-reduced and materially
    SMALLER than the model's real window. If raw_context_window() collapsed
    back to effective_trigger, this assertion would fail (they'd be equal)."""
    from reyn.services.compaction.engine import compute_budgets

    model = "openai/gpt-4o"
    cfg = CompactionConfig()
    budgets = compute_budgets(cfg, model, T_SP=2000, T_comp_SP=500)

    class _FakeEngine:
        def __init__(self, b):
            self.budgets = b

    class _FakeController:
        def __init__(self, engine):
            self._engine = engine

    advisor = _make_advisor(
        model=model, compaction_controller=_FakeController(_FakeEngine(budgets)),
    )
    raw = advisor.raw_context_window()["window"]
    adjusted = advisor.context_window_status()["effective_trigger"]
    assert raw == get_max_input_tokens(model)
    assert adjusted == budgets.effective_trigger
    assert raw > adjusted, (
        "the model's real window must be strictly larger than the "
        "compaction-adjusted trigger threshold — otherwise the two figures "
        "would be indistinguishable and the chip's split has no purpose"
    )


def test_raw_context_window_live_reads_model_fn() -> None:
    """Tier 2: like context_window_status, raw_context_window reads the model
    live via model_fn (not cached at construction) — a /model override must be
    reflected without rebuilding the advisor."""
    current = {"m": "openai/gpt-4o"}
    advisor = ContextBudgetAdvisor(
        compaction=CompactionConfig(),
        compaction_controller=None,
        media_store=None,
        model_fn=lambda: current["m"],
        events=None,
        history_fn=lambda: [],
    )

    base = advisor.raw_context_window()["window"]
    current["m"] = "openai/gpt-4"
    after = advisor.raw_context_window()["window"]
    assert after != base
    assert after == get_max_input_tokens("openai/gpt-4")
