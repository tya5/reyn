"""Tier 2: OS invariant — turn_budget foundation (#1092 force-close + handoff, PR-A).

The cumulative-axis service derives a layer-1 force-close threshold and measures
the wrap-up system prompt cost. These pin the foundation contract BEFORE any
wiring (the trigger hook / force-close call / handoff land in later PRs):

- the threshold IS ``T_max − T_wrap_SP − output_reserve − offload_cap`` (§5);
- ``should_force_close`` is a clean ≥-threshold boundary;
- the threshold is monotone-decreasing in every reserve term (a bigger reserve
  → force-close sooner — the property the retry-guarantee leans on);
- the engine resolves a model CLASS before the catalog lookup (#1172 discipline
  shared with CompactionEngine — an unresolved class silently mis-budgets);
- ``assert_turn_budget_bounds`` fails fast on a degenerate config rather than
  force-closing on every turn.

No mocks: real ``ModelResolver`` instances + the real model-budget catalog +
the real ``estimate_tokens``. Public surface only (the dataclass fields,
``should_force_close``, the module functions).
"""
from __future__ import annotations

import pytest

from reyn.llm.model_resolver import ModelResolver
from reyn.services.compaction.engine import estimate_tokens
from reyn.services.turn_budget import (
    TurnBudget,
    TurnBudgetEngine,
    assert_turn_budget_bounds,
    compute_turn_budget,
    wrap_up_system_prompt,
)

_MODEL = "gpt-4o-mini"  # a real catalog entry (T_max looked up, not hardcoded)


# ── threshold formula (§5) ───────────────────────────────────────────────────


def test_threshold_is_max_input_minus_all_reserves() -> None:
    """Tier 2: force_close_threshold == T_max − T_wrap_SP − output_reserve −
    offload_cap. Asserted as a RELATIONSHIP against the returned ``max_input``
    so it holds regardless of the catalog's actual T_max for the model."""
    b = compute_turn_budget(
        _MODEL, T_wrap_SP=200, output_reserve=4000, offload_cap=8000
    )
    assert b.force_close_threshold == b.max_input - 200 - 4000 - 8000
    assert b.T_wrap_SP == 200
    assert b.output_reserve == 4000
    assert b.offload_cap == 8000


def test_should_force_close_boundary() -> None:
    """Tier 2: should_force_close is False strictly below the threshold and True
    at and above it (≥ boundary — at-threshold means the next increment would
    cross, so close now)."""
    eng = TurnBudgetEngine(_MODEL, output_reserve=4000, offload_cap=8000)
    t = eng.budget.force_close_threshold
    assert eng.should_force_close(t - 1) is False
    assert eng.should_force_close(t) is True
    assert eng.should_force_close(t + 1) is True


def test_threshold_monotone_decreasing_in_each_reserve() -> None:
    """Tier 2: a larger T_wrap_SP / output_reserve / offload_cap each lowers the
    threshold (force-close sooner). Monotonicity is the property the layer-2
    retry-guarantee relies on — shrinking accumulated content can only move you
    back under the threshold, never past it."""
    base = compute_turn_budget(
        _MODEL, T_wrap_SP=100, output_reserve=1000, offload_cap=1000
    )
    bigger_sp = compute_turn_budget(
        _MODEL, T_wrap_SP=500, output_reserve=1000, offload_cap=1000
    )
    bigger_out = compute_turn_budget(
        _MODEL, T_wrap_SP=100, output_reserve=5000, offload_cap=1000
    )
    bigger_off = compute_turn_budget(
        _MODEL, T_wrap_SP=100, output_reserve=1000, offload_cap=5000
    )
    assert bigger_sp.force_close_threshold < base.force_close_threshold
    assert bigger_out.force_close_threshold < base.force_close_threshold
    assert bigger_off.force_close_threshold < base.force_close_threshold


# ── wrap-up SP ───────────────────────────────────────────────────────────────


def test_engine_measures_wrap_up_sp() -> None:
    """Tier 2: the engine measures T_wrap_SP via estimate_tokens on the wrap-up
    SP (the same way CompactionEngine measures T_comp_SP), and exposes that SP."""
    eng = TurnBudgetEngine(_MODEL, output_reserve=4000, offload_cap=8000)
    assert eng.budget.T_wrap_SP > 0
    assert eng.budget.T_wrap_SP == estimate_tokens(wrap_up_system_prompt(), _MODEL)
    assert eng.wrap_up_sp == wrap_up_system_prompt()


def test_wrap_up_sp_instructs_consolidate_and_stop() -> None:
    """Tier 2: the wrap-up SP carries the role-switch CONTRACT — consolidate and
    stop, not continue, and name the §4 facets. Pinned by semantic substring
    only (NOT exact text / length / whitespace, which would be Tier 4)."""
    lowered = wrap_up_system_prompt().lower()
    assert "wrap up" in lowered
    assert "do not continue" in lowered
    # §4 structure: the consolidation facets are named (done / remaining / repeat).
    for facet in ("done", "remain", "repeat"):
        assert facet in lowered


# ── #1172 resolver discipline ────────────────────────────────────────────────


def test_resolver_applied_to_model_class_before_catalog_lookup() -> None:
    """Tier 2: a model CLASS is resolved to its LiteLLM string before the catalog
    lookup — so compute_turn_budget("myclass", resolver=...) sees the SAME T_max
    as the resolved literal model. Without resolution the class would fall through
    to get_max_input_tokens' 128K default and mis-budget (the #1172 trap)."""
    resolver = ModelResolver({"myclass": _MODEL})
    via_class = compute_turn_budget(
        "myclass", T_wrap_SP=200, output_reserve=4000, offload_cap=8000,
        resolver=resolver,
    )
    via_literal = compute_turn_budget(
        _MODEL, T_wrap_SP=200, output_reserve=4000, offload_cap=8000,
    )
    assert via_class.max_input == via_literal.max_input
    assert via_class.force_close_threshold == via_literal.force_close_threshold


def test_engine_resolves_model_class_for_wrap_sp_and_budget() -> None:
    """Tier 2: the engine, like CompactionEngine, resolves the class at init so
    both the T_wrap_SP measurement and the budget use the real model."""
    resolver = ModelResolver({"myclass": _MODEL})
    via_class = TurnBudgetEngine(
        "myclass", output_reserve=4000, offload_cap=8000, resolver=resolver
    )
    via_literal = TurnBudgetEngine(_MODEL, output_reserve=4000, offload_cap=8000)
    assert via_class.budget.max_input == via_literal.budget.max_input
    assert via_class.budget.T_wrap_SP == via_literal.budget.T_wrap_SP


# ── fail-fast bounds (sibling of compaction assert_static_bounds) ─────────────


def test_assert_bounds_rejects_nonpositive_threshold() -> None:
    """Tier 2: a config whose reserves exceed T_max (threshold ≤ 0) is rejected
    fail-fast — otherwise the engine would force-close on every single turn."""
    bad = TurnBudget(
        max_input=1000, T_wrap_SP=200, output_reserve=900, offload_cap=200,
        force_close_threshold=1000 - 200 - 900 - 200,  # = -300
    )
    assert bad.force_close_threshold <= 0
    with pytest.raises(AssertionError):
        assert_turn_budget_bounds(bad)


def test_assert_bounds_rejects_unmeasured_wrap_sp() -> None:
    """Tier 2: T_wrap_SP must be > 0 (a zero means the SP was never measured)."""
    bad = TurnBudget(
        max_input=128000, T_wrap_SP=0, output_reserve=4000, offload_cap=8000,
        force_close_threshold=128000 - 0 - 4000 - 8000,
    )
    with pytest.raises(AssertionError):
        assert_turn_budget_bounds(bad)


def test_engine_init_fails_fast_on_degenerate_config() -> None:
    """Tier 2: TurnBudgetEngine asserts its bounds at construction, so an
    impossible reserve fails at init rather than at first force-close."""
    with pytest.raises(AssertionError):
        TurnBudgetEngine(_MODEL, output_reserve=10**9, offload_cap=0)


def test_well_formed_engine_passes_bounds() -> None:
    """Tier 2: a normal config yields a positive threshold and constructs."""
    eng = TurnBudgetEngine(_MODEL, output_reserve=4000, offload_cap=8000)
    assert eng.budget.force_close_threshold > 0
    assert_turn_budget_bounds(eng.budget)  # does not raise
