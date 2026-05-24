"""Tier 2: R-D8 L4 — BudgetTracker.save_state / load_state.

PR22's BudgetTracker is in-memory; PR25 added daily/monthly persistence
via budget_ledger.jsonl. R-D8 closes the gap for the remaining
in-memory counters (per-agent tokens/cost, per-chain-skill calls/tokens)
so cap enforcement survives a crash + restart cycle.

The state file (``.reyn/state/budget_state.json``) is overwritten
atomically on every record_llm call, hydrated at startup.

Volatile state intentionally NOT persisted:
  - rate-limit window (time-based, 60s — older entries already invalid)
  - warning state (operational dedup, fine to re-warn after restart)
"""
from __future__ import annotations

import json

from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
from reyn.llm.pricing import TokenUsage


def _tracker(per_agent_tokens: int | None = None) -> BudgetTracker:
    cfg = CostConfig(
        per_agent_tokens=CostLimitConfig(hard_limit=per_agent_tokens),
    )
    return BudgetTracker(cfg)


def test_save_state_creates_json_file(tmp_path):
    """Tier 2: save_state writes a JSON file at the given path."""
    bt = _tracker()
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))
    state_path = tmp_path / "budget_state.json"

    bt.save_state(state_path)

    assert state_path.is_file()
    data = json.loads(state_path.read_text())
    assert isinstance(data, dict)


def test_save_state_includes_per_agent_counters(tmp_path):
    """Tier 2: per-agent tokens and cost are recorded."""
    bt = _tracker()
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))
    bt.record_llm(model="gpt-4", agent="beta", usage=TokenUsage(200, 80))
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    data = json.loads(state_path.read_text())
    assert data["agent_tokens"]["alpha"] == 150
    assert data["agent_tokens"]["beta"] == 280


def test_save_state_includes_per_chain_skill_counters(tmp_path):
    """Tier 2: tuple-keyed chain_skill counters are persisted."""
    bt = _tracker()
    # record_spawn increments calls (sub-skill spawn); record_llm increments
    # tokens. Use both to populate the chain_skill counters.
    bt.record_spawn(chain_id="c1", skill="my_skill")
    bt.record_llm(
        model="gpt-4", agent="alpha", usage=TokenUsage(100, 50),
        chain_id="c1", skill="my_skill",
    )
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    data = json.loads(state_path.read_text())
    chain_skill_calls = data.get("chain_skill_calls", [])
    matched = [
        e for e in chain_skill_calls
        if (isinstance(e, list) and e[:2] == ["c1", "my_skill"])
    ]
    assert matched, (
        f"chain_skill_calls must include c1/my_skill; got {chain_skill_calls}"
    )
    chain_id, skill_name, calls = matched[0]
    assert calls == 1
    # tokens are also persisted
    chain_skill_tokens = data.get("chain_skill_tokens", [])
    matched_tokens = [
        e for e in chain_skill_tokens
        if (isinstance(e, list) and e[:2] == ["c1", "my_skill"])
    ]
    assert matched_tokens
    _, _, tokens = matched_tokens[0]
    assert tokens == 150


def test_load_state_restores_per_agent_counters(tmp_path):
    """Tier 2: load_state populates agent counters from disk."""
    bt = _tracker()
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    bt2 = _tracker()
    bt2.load_state(state_path)

    snap = bt2.snapshot()
    assert snap["agent_tokens"]["alpha"] == 150


def test_load_state_restores_chain_skill_counters(tmp_path):
    """Tier 2: load_state populates chain_skill counters."""
    bt = _tracker()
    bt.record_spawn(chain_id="c1", skill="s1")
    bt.record_spawn(chain_id="c1", skill="s1")
    bt.record_llm(
        model="gpt-4", agent="alpha", usage=TokenUsage(100, 50),
        chain_id="c1", skill="s1",
    )
    bt.record_llm(
        model="gpt-4", agent="alpha", usage=TokenUsage(50, 20),
        chain_id="c1", skill="s1",
    )
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    bt2 = _tracker()
    bt2.load_state(state_path)
    snap = bt2.snapshot()

    assert snap["chain_skill_calls"]["c1/s1"] == 2
    # 100+50+50+20 = 220 tokens
    assert snap["chain_skill_tokens"]["c1/s1"] == 220


def test_load_state_missing_file_is_noop(tmp_path):
    """Tier 2: load_state on a missing file leaves the tracker pristine."""
    bt = _tracker()
    bt.load_state(tmp_path / "does_not_exist.json")  # must not raise
    snap = bt.snapshot()
    assert snap["agent_tokens"] == {}
    assert snap["chain_skill_calls"] == {}


def test_load_state_corrupt_file_is_noop(tmp_path):
    """Tier 2: load_state on corrupt JSON leaves the tracker pristine.

    Defensive: corrupt state file (partial write, manual edit gone bad)
    should not crash startup. Operator can use --reset to recover.
    """
    state_path = tmp_path / "budget_state.json"
    state_path.write_text("not valid json {{{")
    bt = _tracker()
    bt.load_state(state_path)  # must not raise
    snap = bt.snapshot()
    assert snap["agent_tokens"] == {}


def test_save_state_uses_atomic_write(tmp_path):
    """Tier 2: save_state writes via tmp + rename (no partial files left).

    Pinned because a crash mid-write must leave the previous file
    intact — same atomic-write contract as snapshot.save.
    """
    bt = _tracker()
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    # No leftover .tmp file
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"atomic write must clean up tmp; found {tmp_files}"


def test_round_trip_with_caps(tmp_path):
    """Tier 2: cap config is shared, but counters survive load.

    Cap accuracy across crash: pre-crash $0.80, restart with same
    cap=$1.00, post-resume can only spend $0.20 before refused.
    """
    cfg = CostConfig(per_agent_tokens=CostLimitConfig(hard_limit=300))
    bt = BudgetTracker(cfg)
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(150, 100))
    state_path = tmp_path / "budget_state.json"
    bt.save_state(state_path)

    # New tracker with same cap, restored state
    bt2 = BudgetTracker(cfg)
    bt2.load_state(state_path)

    # Tokens used: 250 of 300. Next call of 60 would push over.
    check = bt2.check_pre_llm(model="gpt-4", agent="alpha")
    assert check.allowed, "250 < 300 cap should still be allowed"

    bt2.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 20))
    # Now at 310 > 300; next call should be refused
    check2 = bt2.check_pre_llm(model="gpt-4", agent="alpha")
    assert not check2.allowed, (
        "310 tokens used > 300 cap should refuse next call"
    )
