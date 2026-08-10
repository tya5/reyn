"""Tier 2c: #1911 — per-agent token/cost caps survive a crash that lands
inside the throttled-save window.

Quota-enforcement-across-crash invariant. Before #1911 the per-agent
(``_agent_tokens`` / ``_agent_cost_usd``) counters persisted ONLY via the
throttled, best-effort ``budget_state.json`` save (``_save_throttle_secs``
default 1.0s, OSError swallowed). ``hydrate`` rebuilt daily / monthly from the
durable fsync-per-append ``BudgetLedger`` but NOT per-agent. A crash inside the
throttle window therefore lost the unsaved increments → caps UNDER-count on
recovery → over-budget LLM calls get re-allowed.

The fix makes the durable ledger the source of truth: ``hydrate`` re-aggregates
per-agent tokens/cost (summed per ``agent`` field) from the ledger.

These tests drive the *production* recovery path (``hydrate`` then
``load_state``, as wired in chat.py / web/deps.py / mcp.py) and assert the caps
are PRESERVED — bounded by construction, the durable ledger record is the
source of truth.

The final test is a migration-safety gate: the per-chain skill-spawn cap (and
its ``kind="spawn"`` ledger records) was removed with the skill machinery, but
a pre-existing on-disk ledger may still contain those legacy records — hydrate
must tolerate them (they contribute nothing) without breaking recovery of the
kept LLM counters.

FALSIFICATION (verified out-of-band on pre-fix HEAD): with hydrate not
restoring per-agent, the same scenario recovers agent_tokens=100 (want 280) —
i.e. that assertion goes RED.
"""
from __future__ import annotations

import json
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
    first record's snapshot, while the durable ledger fsyncs every record.
    That is precisely the "crash before the throttled save lands" gap.
    """
    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)
    bt.set_state_path(state_path, throttle_secs=10_000.0)  # effectively never re-saves

    # 3 LLM calls (100 + 100 + 80 = 280 tokens).
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 50))
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(50, 50))
    bt.record_llm(model="gpt-4", agent="alpha", usage=TokenUsage(40, 40))

    # Sanity: in-memory truth before the crash.
    snap = bt.snapshot()
    assert snap["agent_tokens"]["alpha"] == 280
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


def test_hydrate_tolerates_legacy_spawn_records(tmp_path):
    """Tier 2c: a pre-existing ledger containing legacy skill-spawn records
    (``kind="spawn"``) still hydrates the live LLM counters correctly after
    the skill-dependent hydrate-tolerance branch was removed.

    The per-chain skill-spawn cap, its ``append_spawn`` writer, and the
    ``kind == "spawn"`` skip branch in ``hydrate`` were all removed with the
    skill machinery (no live producer writes such records any more). This
    gate proves the removal is safe: the hydrate loop is *natively* robust to
    the legacy record shape — a record with no ``tokens``/``cost_usd`` and no
    string ``agent`` contributes 0 and is not aggregated — so an old on-disk
    ledger still hydrates. hydrate must (i) not raise on the legacy records,
    (ii) reconstruct the per-agent token/cost totals from the LLM-call records
    only, and (iii) not resurrect any per-chain skill-spawn state.

    FALSIFICATION: if the hydrate loop were not robust to unknown record
    shapes (e.g. it indexed a required field), a legacy spawn record would
    raise here — so this test would go RED, catching an unsafe removal.
    """
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    # Hand-write a legacy ledger: LLM-call records interleaved with legacy
    # spawn records (the shape the removed ``append_spawn`` used to write).
    records = [
        {"ts": "2026-05-02T10:00:00+09:00", "agent": "alpha",
         "model": "gpt-4", "tokens": 100, "cost_usd": 0.01},
        {"ts": "2026-05-02T10:00:01+09:00", "kind": "spawn",
         "chain_id": "c1", "skill": "s1"},
        {"ts": "2026-05-02T10:00:02+09:00", "agent": "alpha",
         "model": "gpt-4", "tokens": 80, "cost_usd": 0.008},
        {"ts": "2026-05-02T10:00:03+09:00", "kind": "spawn",
         "chain_id": "c1", "skill": "s1"},
    ]
    ledger_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    bt = BudgetTracker(_cfg())
    bt.hydrate(ledger_path)  # (i) must not raise on the legacy spawn records
    snap = bt.snapshot()
    # (ii) per-agent total (all-time cumulative) is reconstructed from the two
    # LLM-call records only; the spawn records contribute nothing.
    assert snap["agent_tokens"]["alpha"] == 180
    assert round(snap["agent_cost_usd"]["alpha"], 3) == 0.018
    # (iii) no per-chain skill-spawn state resurfaces.
    assert "chain_skill_calls" not in snap
    assert "chain_skill_tokens" not in snap


if __name__ == "__main__":
    import sys

    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
