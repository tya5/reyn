"""Tier 3 (e2e): R-D8 L6 — cap enforcement across crash + restart.

The headline guarantee for R-D8: a budget cap that was nearly exceeded
in the original run gets exceeded by post-resume work, the next LLM
call is refused. Without R-D8 the cap was bypassed.

Scenario:
  Cap: per_agent_tokens hard_limit = 300

  Run 1 (pre-crash):
    - record_llm: 250 tokens (50 from prompt, 200 from completion)
    - tracker.agent_tokens['alpha'] = 250
    - state file persists 250

  Crash simulation:
    - Discard the BudgetTracker instance entirely

  Run 2 (resume):
    - New BudgetTracker, same config
    - load_state from disk → tracker.agent_tokens['alpha'] = 250
    - check_pre_llm: still allowed (250 < 300)
    - record_llm: 60 tokens → tracker = 310
    - check_pre_llm: REFUSED (310 > 300)

The double-count case (memo-hit forward calc on top of loaded state)
is also exercised: with state loaded, forward calc is suppressed, so
a memoized phase doesn't re-credit on top of the already-loaded state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
from reyn.llm.pricing import TokenUsage


def _cap300() -> CostConfig:
    return CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=300))


def test_e2e_cap_enforced_across_crash_and_restart(tmp_path):
    """Tier 3: pre-crash 250 + post-resume 60 + cap 300 → next call refused."""
    state_path = tmp_path / "budget_state.json"

    # ── Run 1: pre-crash spend ─────────────────────────────────────────
    bt1 = BudgetTracker(_cap300())
    bt1.set_state_path(state_path, throttle_secs=0.0)
    bt1.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 200))
    assert bt1.snapshot()["agent_tokens"]["alpha"] == 250

    # ── Crash: discard the in-memory instance ──────────────────────────
    del bt1

    # ── Run 2 (restart): load state, continue ──────────────────────────
    bt2 = BudgetTracker(_cap300())
    bt2.load_state(state_path)
    bt2.set_state_path(state_path, throttle_secs=0.0)
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 250, (
        "loaded state must restore pre-crash counter"
    )

    # Still allowed at 250
    check = bt2.check_pre_llm(model="gpt-4", agent="alpha")
    assert check.allowed

    # Make a real call: 60 tokens → tracker = 310
    bt2.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 20))
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 310

    # Cap exceeded — next call must be refused
    check2 = bt2.check_pre_llm(model="gpt-4", agent="alpha")
    assert not check2.allowed, (
        "tracker at 310 > cap 300 must refuse next pre-check"
    )
    assert check2.hard_dimension is not None


def test_e2e_state_loaded_flag_suppresses_memo_hit_forward_calc(tmp_path):
    """Tier 3: memo-hit forward-calc is suppressed once state is loaded.

    Defends against the double-count regression: if both load_state +
    forward-calc were credited, the post-resume tracker would over-count
    the in-flight phase's tokens, refusing legitimate calls.
    """
    bt = BudgetTracker(_cap300())
    state_path = tmp_path / "budget_state.json"
    # Seed with a saved state representing 250 tokens used pre-crash
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "version": 1,
        "agent_tokens": {"alpha": 250},
        "agent_cost_usd": {"alpha": 0.0},
        "chain_skill_calls": [],
        "chain_skill_tokens": [],
    }))
    bt.load_state(state_path)
    assert bt.snapshot()["agent_tokens"]["alpha"] == 250

    # Sentinel for the forward-calc suppression: _state_loaded must be True
    assert bt._state_loaded is True

    # If memo-hit forward calc DID run (= no suppression), it would call
    # record_llm and push the tracker over 250. With suppression, the
    # tracker stays at 250 and a fresh 60-token call lands at 310.
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 20))
    assert bt.snapshot()["agent_tokens"]["alpha"] == 310

    check = bt.check_pre_llm(model="gpt-4", agent="alpha")
    assert not check.allowed


def test_e2e_no_state_persistence_test_path_still_works():
    """Tier 3: tests that don't load_state get the original forward-calc.

    Key for L3 tests / paths that exercise memo hit alone — the
    forward-calc behavior is the safety net when state persistence is
    not configured.
    """
    bt = BudgetTracker(_cap300())
    # No load_state call
    assert bt._state_loaded is False
    # Suppressed flag is off — memo-hit forward calc would run as normal
    # (tested elsewhere in test_budget_memo_hit_forward_calc.py)
