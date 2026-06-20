"""Tier 2: BudgetTracker.load_state coerces malformed persisted counters.

The budget state file is written atomically but ``load_state`` does NOT validate
its ``version`` field, so a version-skewed / hand-edited file may carry a null /
non-numeric counter. The per-value ``int(v)`` / ``float(v)`` then crashed the
load. Coerce-to-default keeps the load resilient (mirrors the #1906 pattern; the
ledger-sum path is already ``isinstance``-guarded).

Policy: real BudgetTracker(CostConfig()), temp state file, public snapshot()
surface (no private-state assert), no mocks. Tier line first.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.runtime.budget.budget import BudgetTracker, CostConfig


def test_load_state_malformed_counters_no_crash(tmp_path: Path) -> None:
    """Tier 2: null / non-numeric persisted counters → defaulted, no crash; valid
    values preserved."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "version": 1,
        "agent_tokens": {"a": None, "b": "xyz", "c": 5},
        "agent_cost_usd": {"a": None, "b": 1.5},
        "chain_skill_calls": [["c1", "s1", None], ["c2", "s2", 3]],
    }))

    bt = BudgetTracker(CostConfig())
    bt.load_state(p)  # must not raise

    snap = bt.snapshot()
    assert snap["agent_tokens"]["a"] == 0      # null → 0
    assert snap["agent_tokens"]["b"] == 0      # non-numeric → 0
    assert snap["agent_tokens"]["c"] == 5      # valid preserved
    assert snap["agent_cost_usd"]["a"] == 0.0  # null → 0.0
    assert snap["agent_cost_usd"]["b"] == 1.5  # valid preserved
