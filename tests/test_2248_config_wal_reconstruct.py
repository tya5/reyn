"""Tier 2: OS invariant — #2259 config-registry rewind-reconstruction (generation model).

Recovery-core `.reyn/config` registries are rewind-durable: `record_config_change` records a
full-state config GENERATION keyed by the WAL head, filed under the registry's `.reyn`-relative
path. `_reconcile_config_as_of_cut` reconstructs each generation-tracked config path AS-OF-CUT
(latest-≤-cut wins, full content, no delta-fold) — so the `.yaml` is a DERIVED projection
re-materialised from the generation truth, never an independent source of truth (the #2259
load-bearing invariant). Each generation is a truncation-surviving base (unlike the former
`config_changed` WAL event the truncation could drop).

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
    """Tier 2: per config path the LATEST generation with seq ≤ cut wins — the yaml is
    re-materialised to that FULL content, NOT the on-disk (post-cut) state. RED if reconcile
    trusted the on-disk yaml as the source of truth, or used the absolute-latest generation."""
    reg = _make_registry(tmp_path)
    await reg.record_config_change("config/mcp.yaml", {"mcp": {"servers": {"a": {"command": "x"}}}})
    cut = reg.state_log.current_seq
    # bump the WAL head so the later mutation files a DISTINCT generation (seq > cut):
    await reg.state_log.append("inbox_put", n=0)
    # a later mutation (after the cut) — the live on-disk state the op would have written:
    await reg.record_config_change("config/mcp.yaml", {"mcp": {"servers": {"a": {}, "b": {}}}})
    p = tmp_path / ".reyn" / "config" / "mcp.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump({"mcp": {"servers": {"a": {}, "b": {}}}}), encoding="utf-8")

    reg._reconcile_config_as_of_cut(cut)

    assert _read_yaml(p) == {"mcp": {"servers": {"a": {"command": "x"}}}}, \
        "yaml must be re-materialised from the generation ≤ cut, not the live post-cut state"


@pytest.mark.asyncio
async def test_config_path_first_written_after_cut_is_removed(tmp_path):
    """Tier 2: a config path whose FIRST generation is AFTER the cut did not exist
    as-of-cut → reconcile removes its yaml. RED if a registry created after a rewind point
    survived a rewind to before it existed."""
    reg = _make_registry(tmp_path)
    # bump the head so the only generation is filed at seq > 0 (the cut below).
    await reg.state_log.append("inbox_put", n=0)
    await reg.record_config_change("config/cron.yaml", {"cron": {"jobs": [{"name": "j"}]}})
    p = tmp_path / ".reyn" / "config" / "cron.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump({"cron": {"jobs": [{"name": "j"}]}}), encoding="utf-8")

    reg._reconcile_config_as_of_cut(0)  # cut BEFORE the only generation

    assert not p.exists(), "a config path first written after the cut is removed on reconstruct"


@pytest.mark.asyncio
async def test_record_config_change_records_durable_generation(tmp_path):
    """Tier 2: record_config_change writes a durable full-state generation carrying the content
    (the recovery truth) reconstructable as-of-cut. RED if the seam didn't record the change —
    config would be invisible to reconstruct = a silent recovery gap."""
    reg = _make_registry(tmp_path)
    await reg.record_config_change("config/hooks.yaml", {"hooks": [{"on": "turn_start"}]})
    cut = reg.state_log.current_seq

    p = tmp_path / ".reyn" / "config" / "hooks.yaml"
    reg._reconcile_config_as_of_cut(cut)
    assert _read_yaml(p) == {"hooks": [{"on": "turn_start"}]}, \
        "the recorded generation must reconstruct the content as-of-cut"
