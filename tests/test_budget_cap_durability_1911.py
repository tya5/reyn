"""Tier 2c: #1911 — per-agent + per-chain spawn caps survive a crash that
lands inside the throttled-save window.

Quota-enforcement-across-crash invariant. Before #1911 the per-agent
(``_agent_tokens`` / ``_agent_cost_usd``) and per-chain spawn-count counters
persisted ONLY via the throttled, best-effort ``budget_state.json`` save
(``_save_throttle_secs`` default 1.0s, OSError swallowed). ``hydrate`` rebuilt
daily / monthly from the durable fsync-per-append ``BudgetLedger`` but NOT
per-agent, and spawn counts had no durable backing at all. A crash inside the
throttle window therefore lost the unsaved increments → caps UNDER-count on
recovery → over-budget LLM calls / skill spawns get re-allowed.

The fix makes the durable ledger the source of truth for both:
  - ``hydrate`` re-aggregates per-agent tokens/cost (summed per ``agent``) and
    per-chain spawn counts (``kind="spawn"`` records) from the ledger.
  - ``record_spawn`` appends a fsync'd spawn record (not just the throttled
    save).

These tests drive the *production* recovery path (``hydrate`` then
``load_state``, as wired in chat.py / web/deps.py / mcp.py) and assert the
caps are PRESERVED — bounded by construction, the durable ledger record is the
source of truth.

FALSIFICATION (verified out-of-band on pre-fix HEAD): with hydrate not
restoring per-agent and record_spawn not appending to the ledger, the same
scenario recovers agent_tokens=100 (want 280) and chain_skill_calls={} (want
2) — i.e. these assertions go RED.
"""
from __future__ import annotations

from pathlib import Path

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig, CostLimitConfig


def _cfg() -> CostConfig:
    # Caps chosen so the post-crash totals sit just under the hard limit:
    # only a correct (non-under-counted) recovery enforces the cap.
    return CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=300))


def _simulate_crash_in_throttle_window(
    ledger_path: Path, state_path: Path
) -> BudgetTracker:
    """Run pre-crash activity whose later increments never reach the throttled
    state save, then return the tracker (caller discards it = the crash).

    ``set_state_path`` makes the *first* record always save (throttle clock at
    0), so a large throttle window means: the state file captures only the
    first record's snapshot, while the durable ledger fsyncs every record /
    spawn. That is precisely the "crash before the throttled save lands" gap.
    """
    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=10_000.0)  # effectively never re-saves

    # 3 LLM calls (100 + 100 + 80 = 280 tokens) and 2 spawns, interleaved.
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 50))
    bt.record_spawn(chain_id="c1", skill="s1")
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 50))
    bt.record_spawn(chain_id="c1", skill="s1")
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 40))

    # Sanity: in-memory truth before the crash.
    snap = bt.snapshot()
    assert snap["agent_tokens"]["alpha"] == 280
    assert snap["chain_skill_calls"]["c1/s1"] == 2
    return bt


def _recover(ledger_path: Path, state_path: Path) -> BudgetTracker:
    """Reconstruct a tracker via the production recovery path.

    Mirrors the wiring in chat.py / web/deps.py / mcp.py: hydrate (durable
    ledger) first, then load_state (throttled best-effort cache).
    """
    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.load_state(state_path)
    return bt


def test_per_agent_tokens_preserved_across_crash(tmp_path):
    """Tier 2c: per-agent token counter is not under-counted after a crash
    inside the throttle window (durable ledger restores the full total)."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    bt = _simulate_crash_in_throttle_window(ledger_path, state_path)
    del bt  # crash: discard in-memory state

    bt2 = _recover(ledger_path, state_path)
    snap = bt2.snapshot()
    assert snap["agent_tokens"]["alpha"] == 280, (
        "per-agent tokens must be restored from the durable ledger, not the "
        "stale throttled state file"
    )


def test_per_chain_spawn_count_preserved_across_crash(tmp_path):
    """Tier 2c: per-chain spawn count is not under-counted after a crash inside
    the throttle window (record_spawn appends a durable ledger record)."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    bt = _simulate_crash_in_throttle_window(ledger_path, state_path)
    del bt

    bt2 = _recover(ledger_path, state_path)
    snap = bt2.snapshot()
    assert snap["chain_skill_calls"]["c1/s1"] == 2, (
        "per-chain spawn count must be restored from the durable ledger; "
        "spawn counts had no durable backing before #1911"
    )


def test_per_agent_cap_enforced_after_crash(tmp_path):
    """Tier 2c: the per-agent token cap still refuses once the durable total
    crosses the hard limit after recovery.

    With the cap at 300 and a recovered total of 280, a 40-token call lands at
    320 and the next pre-check must refuse. An under-counted recovery (100)
    would wrongly keep allowing calls = the cap bypass this guards against.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    bt = _simulate_crash_in_throttle_window(ledger_path, state_path)
    del bt

    bt2 = _recover(ledger_path, state_path)
    # 280 < 300 → still allowed.
    assert bt2.check_pre_llm(model="gpt-4", agent="alpha").allowed
    # One more real call pushes over the cap.
    bt2.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(20, 20))
    assert bt2.snapshot()["agent_tokens"]["alpha"] == 320
    check = bt2.check_pre_llm(model="gpt-4", agent="alpha")
    assert not check.allowed, "320 > cap 300 must refuse the next call after recovery"
    assert check.hard_dimension == "per_agent_tokens"


def test_per_chain_spawn_cap_enforced_after_crash(tmp_path):
    """Tier 2c: the per-chain spawn cap still refuses once the durable spawn
    count reaches the hard limit after recovery.

    Spawn cap = 2. The two pre-crash spawns are restored from the ledger, so a
    third spawn in the same chain must be refused. An under-counted recovery
    (0 spawns) would wrongly re-allow runaway spawning = the loop-detection
    bypass this guards against.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    # safety.loop.skill_calls_per_chain drives the per-chain spawn cap.
    class _Loop:
        skill_calls_per_chain = CostLimitConfig(hard_limit=2)
        skill_tokens_per_chain = CostLimitConfig()

    class _Safety:
        loop = _Loop()

    # Pre-crash: hydrate + 2 durable spawns, throttled save stays stale.
    bt = BudgetTracker(_cfg(), safety=_Safety())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=10_000.0)
    bt.record_spawn(chain_id="c1", skill="s1")
    bt.record_spawn(chain_id="c1", skill="s1")
    assert bt.snapshot()["chain_skill_calls"]["c1/s1"] == 2
    del bt  # crash

    # Recover with the SAME spawn cap.
    bt2 = BudgetTracker(_cfg(), safety=_Safety())
    bt2.hydrate(ledger_path)
    bt2.load_state(state_path)
    assert bt2.snapshot()["chain_skill_calls"]["c1/s1"] == 2, (
        "spawn count must survive the crash via the durable ledger"
    )
    check = bt2.check_pre_spawn(chain_id="c1", skill="s1")
    assert not check.allowed, (
        "2/2 spawns already used → third spawn must be refused after recovery"
    )
    assert check.hard_dimension == "per_chain_skill_calls"


def test_recovery_does_not_double_count_spawn(tmp_path):
    """Tier 2c: hydrate counts each durable spawn record exactly once.

    The ledger is append-only and hydrate recounts from scratch on every
    restart, so a recover→record→recover sequence must not inflate the count
    (bounded by construction: one ledger record per record_spawn call).
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=10_000.0)
    bt.record_spawn(chain_id="c1", skill="s1")
    del bt

    # First recovery sees exactly 1.
    bt2 = _recover(ledger_path, state_path)
    assert bt2.snapshot()["chain_skill_calls"]["c1/s1"] == 1
    # One more durable spawn, then crash + recover again.
    bt2.set_state_path(state_path, throttle_secs=10_000.0)
    bt2.record_spawn(chain_id="c1", skill="s1")
    del bt2

    bt3 = _recover(ledger_path, state_path)
    assert bt3.snapshot()["chain_skill_calls"]["c1/s1"] == 2, (
        "two record_spawn calls → exactly two; no replay double-count"
    )


def test_spawn_records_excluded_from_period_and_agent_totals(tmp_path):
    """Tier 2c: spawn records carry no tokens/cost, so they must not pollute
    the daily / monthly / per-agent token+cost aggregation in hydrate."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    state_path = tmp_path / ".reyn" / "state" / "budget_state.json"

    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=10_000.0)
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 50))  # 100 tok
    bt.record_spawn(chain_id="c1", skill="s1")
    del bt

    bt2 = _recover(ledger_path, state_path)
    snap = bt2.snapshot()
    # Spawn record contributes 0 tokens; only the LLM call counts.
    assert snap["agent_tokens"]["alpha"] == 100
    assert snap["daily_tokens"] == 100
    assert snap["chain_skill_calls"]["c1/s1"] == 1


if __name__ == "__main__":
    import sys

    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
