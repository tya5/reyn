"""Tier 2: OS invariant — #2405 config-generation rewind-reconstruction symmetric gap.

``_reconcile_config_as_of_cut`` used ``latest_at_or_below(cut=N)`` — the same
symmetric gap as vanish/archive/topology. Post-rewind active config generations
(seq > R > N) were excluded, reverting the config to as-of-N on crash recovery even
when it was legitimately updated on the active post-rewind branch.

Fix: ``ConfigGenerationStore.latest_active`` uses ``is_active_seq`` — the same
active-branch predicate used throughout the rewind reconstruction stack:
• Pre-target (seq ≤ N): ``is_active_seq=True`` → applied.
• Abandoned branch (N < seq < R): ``is_active_seq=False`` → skipped.
• Post-rewind active (seq > R): ``is_active_seq=True`` → applied.

Real AgentRegistry + StateLog + config generation store (no mocks). Each test adds
a rewind record before calling _materialize_rewind (production invariant).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _read_config(tmp_path: Path, rel_path: str) -> dict:
    abs_path = tmp_path / ".reyn" / rel_path
    if not abs_path.is_file():
        return {}
    return yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}


@pytest.mark.asyncio
async def test_config_post_rewind_generation_applied(tmp_path):
    """Tier 2: config generation recorded POST-REWIND (seq > R, active branch) is
    applied on crash recovery — ``is_active_seq=True`` → post-rewind config wins.

    Symmetric gap fix (#2405): ``latest_at_or_below(cut=N)`` excluded post-rewind
    generations, reverting config to as-of-N content even on the active branch.

    Sequence: gen at seq 1 (N, pre-rewind content), R=2 (rewind to N), gen at seq 3
    (post-rewind active, updated content). Recovery must reflect seq-3 content."""
    reg = _make_registry(tmp_path)
    log = reg.state_log
    n_seq = await log.append("inbox_put", target="x", msg_id="m", msg_kind="user",
                             payload={"text": "x"})           # seq 1 = N
    await reg.record_config_change("config/mcp.yaml", {"servers": {}})  # gen at seq 1
    R = await rewind(log, target_n=n_seq)                     # seq 2 = R
    await log.append("inbox_put", target="x", msg_id="m2", msg_kind="user",
                     payload={"text": "y"})                   # seq 3 (post-rewind active)
    # post-rewind active config generation — should be applied on recovery
    await reg.record_config_change("config/mcp.yaml", {"servers": {"new-mcp": {"url": "x"}}})

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=n_seq)

    result = _read_config(tmp_path, "config/mcp.yaml")
    assert "new-mcp" in result.get("servers", {})  # post-rewind content applied


@pytest.mark.asyncio
async def test_config_abandoned_generation_excluded(tmp_path):
    """Tier 2: config generation on the ABANDONED branch (N < seq < R) is excluded on
    crash recovery — ``is_active_seq=False`` → pre-N content (the as-of-N generation)
    wins, not the abandoned-branch update.

    Sequence: gen at seq 1 (N, baseline), gen at seq 2 (abandoned, stale update), R=3
    (rewind to N). Recovery must reflect seq-1 content, not seq-2 abandoned content."""
    reg = _make_registry(tmp_path)
    log = reg.state_log
    n_seq = await log.append("inbox_put", target="x", msg_id="m", msg_kind="user",
                             payload={"text": "x"})           # seq 1 = N
    await reg.record_config_change("config/mcp.yaml", {"servers": {}})  # gen at seq 1 (baseline)
    await log.append("inbox_put", target="x", msg_id="m2", msg_kind="user",
                     payload={"text": "y"})                   # seq 2 (abandoned branch)
    # abandoned-branch config generation — must NOT be applied on recovery
    await reg.record_config_change("config/mcp.yaml", {"servers": {"stale-mcp": {"url": "y"}}})
    R = await rewind(log, target_n=n_seq)                     # seq 3+ = R; seq 2+ in (1,R)

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=n_seq)

    result = _read_config(tmp_path, "config/mcp.yaml")
    assert "stale-mcp" not in result.get("servers", {})  # abandoned gen excluded
    assert result.get("servers") == {}                    # baseline (seq-1) content
