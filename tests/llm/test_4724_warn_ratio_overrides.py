"""Tier 2: #4206 Slice B (#4724) — caller-resolved ③ preference-axis
overrides for the 7 ``cost.*.warn_ratio`` keys.

Design C (lead-coder ruling): the CALLER resolves the override
(session/agent/project composition) and passes a ``dict[str, float]``
(dotted PREFERENCE_KEYS string -> ratio) straight into ``BudgetTracker`` —
no process-shared registry, no session-id plumbing into the tracker. The
tracker's own counters stay PROCESS-SHARED and unaffected; only the ratio
that decides WHEN to warn about an already-shared number is caller-
resolvable.

Real ``BudgetTracker``/``CostConfig`` throughout — no mocks, mirrors
``test_budget_rate_limit_window.py``'s own construction style.
"""
from __future__ import annotations

import pytest

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import (
    _WARN_RATIO_PREFERENCE_KEYS,
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
    _effective_warn_threshold,
    _validate_warn_ratio_overrides,
    format_budget_full,
)
from reyn.runtime.preferences import UnknownPreferenceKeyError


def _record(bt: BudgetTracker, *, model: str = "m", agent: str | None = "a", n: int = 1) -> None:
    for _ in range(n):
        bt.record_llm(model=model, agent=agent, usage=TokenUsage(prompt_tokens=1, completion_tokens=0))


# ── pure helpers ─────────────────────────────────────────────────────────


def test_effective_warn_threshold_uses_override_when_given():
    """Tier 2: an override ratio replaces cap.warn_ratio in the threshold
    computation."""
    cap = CostLimitConfig(hard_limit=100.0, warn_ratio=0.8)
    assert _effective_warn_threshold(cap, 0.5) == 50.0


def test_effective_warn_threshold_falls_back_to_cap_ratio_when_none():
    """Tier 2: (accept-side) no override — byte-identical to the pre-Slice-B
    cap.warn_threshold computation."""
    cap = CostLimitConfig(hard_limit=100.0, warn_ratio=0.8)
    assert _effective_warn_threshold(cap, None) == cap.warn_threshold == 80.0


def test_effective_warn_threshold_none_when_no_hard_limit():
    """Tier 2: (accept-side) an inactive cap (no hard_limit) has no
    threshold regardless of override."""
    cap = CostLimitConfig(hard_limit=None, warn_ratio=0.8)
    assert _effective_warn_threshold(cap, 0.5) is None


def test_effective_warn_threshold_never_mutates_the_shared_cap():
    """Tier 2: (accept-side) computing an effective threshold with an
    override does NOT mutate the CostLimitConfig instance — it is shared
    by every agent/session in the process; leaking one caller's override
    into it would apply it to every OTHER caller too."""
    cap = CostLimitConfig(hard_limit=100.0, warn_ratio=0.8)
    _effective_warn_threshold(cap, 0.1)
    assert cap.warn_ratio == 0.8


def test_warn_ratio_preference_keys_is_the_cost_subset_of_preference_keys():
    """Tier 2: (accept-side) the derived subset is exactly the 7 cost.*
    warn-ratio keys — no drift from PREFERENCE_KEYS's own declaration."""
    assert _WARN_RATIO_PREFERENCE_KEYS == frozenset({
        "cost.per_agent_tokens.warn_ratio",
        "cost.per_agent_cost_usd.warn_ratio",
        "cost.daily_tokens.warn_ratio",
        "cost.daily_cost_usd.warn_ratio",
        "cost.monthly_tokens.warn_ratio",
        "cost.monthly_cost_usd.warn_ratio",
        "cost.rate_limit_warn_ratio",
    })


def test_validate_warn_ratio_overrides_accepts_known_keys():
    """Tier 2: (accept-side) a real cost.* key passes without raising."""
    _validate_warn_ratio_overrides({"cost.per_agent_tokens.warn_ratio": 0.5})


def test_validate_warn_ratio_overrides_accepts_none_and_empty():
    """Tier 2: (accept-side) the common "no override" cases."""
    _validate_warn_ratio_overrides(None)
    _validate_warn_ratio_overrides({})


def test_validate_warn_ratio_overrides_rejects_a_non_cost_preference_key():
    """Tier 2: a key that IS in PREFERENCE_KEYS but NOT in the cost.*
    warn-ratio subset (e.g. output_language) is still rejected — this
    transport only accepts the 7 keys it is the consumer for."""
    with pytest.raises(UnknownPreferenceKeyError, match="output_language"):
        _validate_warn_ratio_overrides({"output_language": 1.0})


def test_validate_warn_ratio_overrides_rejects_a_typo():
    """Tier 2: a typo'd key raises loudly — the #4655 defect class
    reproduced on the transport side, closed the same way."""
    with pytest.raises(UnknownPreferenceKeyError, match="cost.typo.warn_ratio"):
        _validate_warn_ratio_overrides({"cost.typo.warn_ratio": 0.5})


# ── BudgetTracker.record_llm — per-agent warn-crossing ──────────────────


def test_per_agent_tokens_warn_fires_earlier_with_a_tighter_override():
    """Tier 2: THE central witness — an override ratio TIGHTER than the
    project default makes the warn fire at a LOWER usage level. RED
    without the fix: warn never fires until the project-default 80%
    threshold, ignoring the override entirely."""
    cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100.0, warn_ratio=0.8))
    bt = BudgetTracker(cfg)

    # 50 tokens is BELOW the project default's 80-token threshold...
    check = bt.record_llm(
        model="m", agent="a", usage=TokenUsage(prompt_tokens=50, completion_tokens=0),
        warn_ratio_overrides={"cost.per_agent_tokens.warn_ratio": 0.4},  # threshold=40
    )
    # ...but ABOVE the override's 40-token threshold.
    assert "per_agent_tokens" in check.warn_dimensions


def test_per_agent_tokens_warn_does_not_fire_with_no_override():
    """Tier 2: (accept-side) the SAME usage level, no override — stays
    below the project-default threshold, no warn. Confirms the witness
    above is really about the override, not just "any 50-token call warns"."""
    cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100.0, warn_ratio=0.8))
    bt = BudgetTracker(cfg)

    check = bt.record_llm(
        model="m", agent="a", usage=TokenUsage(prompt_tokens=50, completion_tokens=0),
    )
    assert "per_agent_tokens" not in check.warn_dimensions


def test_per_agent_cost_usd_warn_ratio_override():
    """Tier 2: (accept-side) same witness, the cost dimension."""
    cfg = CostConfig(per_agent_cost_usd=CostLimitConfig(hard_limit=1.0, warn_ratio=0.9))
    bt = BudgetTracker(cfg)
    # gpt-4o-mini priced call — exact $ amount doesn't matter, just that
    # SOME cost was recorded; use a model reyn's pricing table covers.
    check = bt.record_llm(
        model="gemini/gemini-2.5-flash-lite", agent="a",
        usage=TokenUsage(prompt_tokens=100_000, completion_tokens=0),
        warn_ratio_overrides={"cost.per_agent_cost_usd.warn_ratio": 0.0001},
    )
    assert "per_agent_cost_usd" in check.warn_dimensions


def test_unrelated_override_key_does_not_affect_a_different_dimension():
    """Tier 2: (accept-side) an override for per_agent_cost_usd does not
    leak into the per_agent_tokens check."""
    cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100.0, warn_ratio=0.8))
    bt = BudgetTracker(cfg)
    check = bt.record_llm(
        model="m", agent="a", usage=TokenUsage(prompt_tokens=50, completion_tokens=0),
        warn_ratio_overrides={"cost.per_agent_cost_usd.warn_ratio": 0.01},
    )
    assert "per_agent_tokens" not in check.warn_dimensions


# ── BudgetTracker._check_rate_limit (via check_pre_llm) ──────────────────


def test_rate_limit_warn_ratio_override_fires_earlier():
    """Tier 2: THE rate-limit witness — a tighter override ratio makes
    the rate-limit warn fire at a lower call count than the project
    default."""
    model = "warn-model"
    cap = 10
    cfg = CostConfig(rate_limit_per_minute={model: cap}, rate_limit_warn_ratio=0.8)
    bt = BudgetTracker(cfg)

    _record(bt, model=model, n=3)  # 3/10 — below the default's 8 threshold, above the override's 2

    result = bt.check_pre_llm(
        model=model, agent=None,
        warn_ratio_overrides={"cost.rate_limit_warn_ratio": 0.2},  # threshold = 2
    )
    assert "rate_limit" in result.warn_dimensions


def test_rate_limit_warn_ratio_no_override_uses_project_default():
    """Tier 2: (accept-side) same call count, no override — stays below
    the project-default (0.8 * 10 = 8) threshold."""
    model = "warn-model-2"
    cap = 10
    cfg = CostConfig(rate_limit_per_minute={model: cap}, rate_limit_warn_ratio=0.8)
    bt = BudgetTracker(cfg)

    _record(bt, model=model, n=3)

    result = bt.check_pre_llm(model=model, agent=None)
    assert "rate_limit" not in result.warn_dimensions


# ── BudgetTracker._check_period_warn (daily/monthly, via record_llm) ─────


def test_daily_tokens_warn_ratio_override():
    """Tier 2: (accept-side) same witness, the daily_tokens period
    dimension. The counter itself stays process-shared/period-cumulative;
    only the ratio deciding when to warn about it is overridden."""
    cfg = CostConfig(daily_tokens=CostLimitConfig(hard_limit=1000.0, warn_ratio=0.9))
    bt = BudgetTracker(cfg)
    check = bt.record_llm(
        model="m", agent=None, usage=TokenUsage(prompt_tokens=300, completion_tokens=0),
        warn_ratio_overrides={"cost.daily_tokens.warn_ratio": 0.2},  # threshold = 200
    )
    assert "daily_tokens" in check.warn_dimensions


def test_daily_tokens_no_override_uses_project_default():
    """Tier 2: (accept-side) same usage, no override — below the
    project-default 90% threshold."""
    cfg = CostConfig(daily_tokens=CostLimitConfig(hard_limit=1000.0, warn_ratio=0.9))
    bt = BudgetTracker(cfg)
    check = bt.record_llm(
        model="m", agent=None, usage=TokenUsage(prompt_tokens=300, completion_tokens=0),
    )
    assert "daily_tokens" not in check.warn_dimensions


# ── format_budget_full (the /budget display) ──────────────────────────────


def test_format_budget_full_warn_marker_reflects_the_override():
    """Tier 2: the /budget display's "warn at" figure and ⚠ marker use the
    caller-resolved override, matching what actually gates this session's
    warn events — not silently the project default."""
    cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=100.0, warn_ratio=0.8))
    bt = BudgetTracker(cfg)
    bt.record_llm(model="m", agent="a", usage=TokenUsage(prompt_tokens=50, completion_tokens=0))
    snap = bt.snapshot()

    text_no_override = format_budget_full(snap, attached="a")
    text_with_override = format_budget_full(
        snap, attached="a",
        warn_ratio_overrides={"cost.per_agent_tokens.warn_ratio": 0.4},
    )

    assert "(warn at 80)" in text_no_override
    assert "(warn at 40)" in text_with_override
    assert "⚠ approaching" in text_with_override
    assert "⚠ approaching" not in text_no_override


def test_format_budget_full_rejects_an_unknown_override_key():
    """Tier 2: (accept-side) the display function validates too — the SAME
    loud-reject contract as check_pre_llm/record_llm, not a silently
    ignored key on the display-only path."""
    cfg = CostConfig()
    bt = BudgetTracker(cfg)
    snap = bt.snapshot()
    with pytest.raises(UnknownPreferenceKeyError):
        format_budget_full(snap, attached="a", warn_ratio_overrides={"cost.bogus.warn_ratio": 0.5})
