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


def test_e2e_loaded_state_does_not_double_count_on_subsequent_records(tmp_path):
    """Tier 2c: after load_state, subsequent record_llm doesn't replay the
    loaded counts on top of themselves.

    Defends the cap-bypass-on-resume invariant: if load_state's contribution
    were counted twice (once by load, once by re-credit somewhere), the
    post-resume tracker would over-count and refuse legitimate calls. The
    behavior verified here is what user-visible cap enforcement depends on.
    """
    bt = BudgetTracker(_cap300())
    state_path = tmp_path / "budget_state.json"
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

    # A fresh 60-token call lands at exactly 310 (250 + 60), not 560
    # (which would indicate double-count via re-credit).
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 20))
    assert bt.snapshot()["agent_tokens"]["alpha"] == 310

    check = bt.check_pre_llm(model="gpt-4", agent="alpha")
    assert not check.allowed
