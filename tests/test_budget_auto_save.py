"""Tier 2: R-D8 L5 — BudgetTracker auto-saves state after record_llm.

Once ``set_state_path(path)`` is called at startup, every subsequent
``record_llm`` triggers a throttled save_state. Without the throttle,
hot LLM call paths would issue dozens of file writes per second.
"""
from __future__ import annotations

import json
import time

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.llm.pricing import TokenUsage


def _tracker() -> BudgetTracker:
    return BudgetTracker(CostConfig())


def test_set_state_path_enables_auto_save(tmp_path):
    """Tier 2: after set_state_path, record_llm writes the state file."""
    bt = _tracker()
    state_path = tmp_path / "budget_state.json"
    # zero throttle for test determinism
    bt.set_state_path(state_path, throttle_secs=0.0)
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))

    assert state_path.is_file()
    data = json.loads(state_path.read_text())
    assert data["agent_tokens"]["alpha"] == 150


def test_no_state_path_no_auto_save(tmp_path):
    """Tier 2: without set_state_path, record_llm doesn't write anything."""
    bt = _tracker()
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(100, 50))

    # No file written anywhere; verify by checking tmp_path is empty
    files = list(tmp_path.iterdir())
    assert files == [], f"unexpected files: {files}"


def test_auto_save_throttle_skips_rapid_saves(tmp_path):
    """Tier 2: throttle window collapses rapid consecutive saves to one disk write.

    Pinned because LLM call paths are hot (many calls per second in
    multi-agent skills); without throttle, fsync per call would be
    a real cost.
    """
    bt = _tracker()
    state_path = tmp_path / "budget_state.json"
    bt.set_state_path(state_path, throttle_secs=10.0)  # large window

    # First call: writes the file
    bt.record_llm(model="gpt-4", agent="a", usage=TokenUsage(100, 50))
    first_mtime = state_path.stat().st_mtime_ns

    # Subsequent rapid calls: throttle applies, no rewrite
    for _ in range(5):
        bt.record_llm(model="gpt-4", agent="a", usage=TokenUsage(10, 5))
    second_mtime = state_path.stat().st_mtime_ns

    assert first_mtime == second_mtime, (
        "throttle should collapse rapid saves; mtime shouldn't change"
    )

    # Counter is still updated correctly in memory
    assert bt.snapshot()["agent_tokens"]["a"] == 150 + 75


def test_auto_save_after_throttle_window(tmp_path, monkeypatch):
    """Tier 2: after the throttle window elapses, the next save lands."""
    bt = _tracker()
    state_path = tmp_path / "budget_state.json"
    bt.set_state_path(state_path, throttle_secs=0.5)

    bt.record_llm(model="gpt-4", agent="a", usage=TokenUsage(100, 50))
    first_data = json.loads(state_path.read_text())
    assert first_data["agent_tokens"]["a"] == 150

    # Sleep past the throttle, then record again
    time.sleep(0.6)
    bt.record_llm(model="gpt-4", agent="a", usage=TokenUsage(50, 20))

    second_data = json.loads(state_path.read_text())
    assert second_data["agent_tokens"]["a"] == 220


def test_record_spawn_also_triggers_auto_save(tmp_path):
    """Tier 2: chain_skill counters are persisted when record_spawn fires."""
    bt = _tracker()
    state_path = tmp_path / "budget_state.json"
    bt.set_state_path(state_path, throttle_secs=0.0)
    bt.record_spawn(chain_id="c1", skill="s1")

    data = json.loads(state_path.read_text())
    triplets = data.get("chain_skill_calls", [])
    matched = [
        e for e in triplets
        if isinstance(e, list) and e[:2] == ["c1", "s1"]
    ]
    assert matched, "record_spawn must trigger auto-save of chain_skill_calls"
    assert matched[0][2] == 1


def test_auto_save_survives_save_io_error(tmp_path, monkeypatch):
    """Tier 2: a save_state I/O error must not propagate (defensive)."""
    bt = _tracker()
    # Path that can't be created (parent is a file, not a dir)
    weird = tmp_path / "blocker"
    weird.write_text("x")
    bad_path = weird / "budget_state.json"

    bt.set_state_path(bad_path, throttle_secs=0.0)
    # Must not raise
    bt.record_llm(model="gpt-4", agent="a", usage=TokenUsage(100, 50))
    # Counter still updated in memory
    assert bt.snapshot()["agent_tokens"]["a"] == 150
