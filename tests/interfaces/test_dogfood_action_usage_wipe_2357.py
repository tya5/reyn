"""Tier 2: #2357 — the dogfood per-scenario wipe targets the LIVE action_usage ledger path.

The live ActionUsageTracker persists to ``.reyn/agents/<name>/action_usage.json`` (session.py). The
dogfood harness's per-scenario wipe previously ``unlink``ed ``.reyn/state/action_usage.jsonl`` — a
stale path that never existed → a silent no-op → hot-list frequency counts BLED across dogfood
scenarios (measurement contamination). The wipe target now comes from a pure ``_scenario_state_targets``
helper so it can't silently drift from the tracker's real path again.
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.cli.commands.dogfood import _scenario_state_targets


def test_wipe_action_usage_targets_live_per_agent_path(tmp_path):
    """Tier 2: #2357 — the wipe's action_usage target is the LIVE per-agent path
    (.reyn/agents/<name>/action_usage.json), NOT the stale .reyn/state/action_usage.jsonl. RED under
    the stale path (the unlink was a silent no-op → hot-list bled)."""
    targets = _scenario_state_targets(tmp_path, "alice")
    assert targets["action_usage"] == tmp_path / ".reyn" / "agents" / "alice" / "action_usage.json"
    # the live tracker's convention (session.py): .json under .reyn/agents/<name>/, NOT .reyn/state/
    rel = targets["action_usage"].relative_to(tmp_path).parts
    assert "state" not in rel and targets["action_usage"].suffix == ".json"
    # co-located with the per-agent history (the live agent home the runner already wipes correctly)
    assert targets["action_usage"].parent == targets["history"].parent


def test_wipe_removes_seeded_ledger_at_live_path(tmp_path):
    """Tier 2: #2357 — a ledger seeded at the wipe's target path is actually removed (behavioral:
    pre-fix the stale-path unlink left a live-path ledger in place → cross-scenario bleed)."""
    p = _scenario_state_targets(tmp_path, "alice")["action_usage"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"grep": 5}', encoding="utf-8")
    p.unlink(missing_ok=True)  # the exact op the wipe performs on this target
    assert not p.exists()
