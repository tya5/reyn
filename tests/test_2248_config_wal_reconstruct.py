"""Tier 2: OS invariant — #2248 PR-A config-registry rewind-reconstruction.

Recovery-core `.reyn/config` registries become rewind-durable: `record_config_change` emits a
`config_changed` WAL event carrying the registry's `.reyn`-relative path + its FULL
post-mutation content. `_reconcile_config_as_of_cut` reconstructs each WAL-tracked config path
AS-OF-CUT (latest-≤-cut wins, full content, no delta-fold) — so the `.yaml` is a DERIVED
projection re-materialised from the WAL truth, never an independent source of truth (the #2248
load-bearing invariant). Mirrors the topology-lifecycle model.

Real AgentRegistry + StateLog + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )


def _read_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None


@pytest.mark.asyncio
async def test_config_reconstructs_to_latest_at_or_below_cut(tmp_path):
    """Tier 2: per config path the LATEST config_changed with seq ≤ cut wins — the yaml is
    re-materialised to that FULL content, NOT the on-disk (post-cut) state. RED if reconcile
    trusted the on-disk yaml as the source of truth, or used the absolute-latest event."""
    reg = _make_registry(tmp_path)
    await reg.record_config_change("mcp.yaml", {"mcp": {"servers": {"a": {"command": "x"}}}})
    cut = reg.state_log.current_seq
    # a later mutation (after the cut) — the live on-disk state the op would have written:
    await reg.record_config_change("mcp.yaml", {"mcp": {"servers": {"a": {}, "b": {}}}})
    p = tmp_path / ".reyn" / "mcp.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump({"mcp": {"servers": {"a": {}, "b": {}}}}), encoding="utf-8")

    reg._reconcile_config_as_of_cut(cut)

    assert _read_yaml(p) == {"mcp": {"servers": {"a": {"command": "x"}}}}, \
        "yaml must be re-materialised from the WAL event ≤ cut, not the live post-cut state"


@pytest.mark.asyncio
async def test_config_path_first_written_after_cut_is_removed(tmp_path):
    """Tier 2: a config path whose FIRST config_changed is AFTER the cut did not exist
    as-of-cut → reconcile removes its yaml. RED if a registry created after a rewind point
    survived a rewind to before it existed."""
    reg = _make_registry(tmp_path)
    await reg.record_config_change("cron.yaml", {"cron": {"jobs": [{"name": "j"}]}})  # seq 1 > cut 0
    p = tmp_path / ".reyn" / "cron.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump({"cron": {"jobs": [{"name": "j"}]}}), encoding="utf-8")

    reg._reconcile_config_as_of_cut(0)  # cut BEFORE the only config event

    assert not p.exists(), "a config path first written after the cut is removed on reconstruct"


@pytest.mark.asyncio
async def test_record_config_change_emits_durable_wal_event(tmp_path):
    """Tier 2: record_config_change appends a durable config_changed carrying path+content
    (the recovery truth). RED if the seam didn't WAL the change — config would be invisible to
    replay = a silent recovery gap."""
    reg = _make_registry(tmp_path)
    await reg.record_config_change("hooks.yaml", {"hooks": [{"on": "turn_start"}]})

    [entry] = [e for e in reg.state_log.iter_from(0) if e.get("kind") == "config_changed"]
    assert entry["path"] == "hooks.yaml"
    assert entry["content"] == {"hooks": [{"on": "turn_start"}]}


@pytest.mark.asyncio
async def test_config_changed_does_not_corrupt_agent_snapshot(tmp_path):
    """Tier 2: config_changed is config-set state, NOT AgentSnapshot STATE — apply_events
    no-ops it (uniform with topology_*). RED if it leaked into snapshot replay."""
    from reyn.core.events.agent_snapshot import AgentSnapshot

    snap = AgentSnapshot.empty("a")
    snap.apply_events([
        {"seq": 1, "kind": "config_changed", "path": "mcp.yaml", "content": {"mcp": {}}},
    ])
    assert snap.inbox == [] and snap.pending_chains == {}, \
        "config_changed must not mutate AgentSnapshot state"
