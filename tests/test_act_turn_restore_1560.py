"""Tier 2: OS invariant — act-turn workspace restore (#1560 PR-2, restore half).

PR-1 captures a per-op `write-tree` into the op-content-log keyed by
`op_seq == CommittedStep.seq` on each `step_completed`. PR-2 is the restore
counterpart: `AgentRegistry.restore_workspace_to_act_turn(target_seq)` reads the
op-content-log (is_active-filtered) + `read-tree`s the latest active tree
`≤ target_seq`, with a boundary-generation fallback.

The load-bearing assertion (closes the 2a-3 runtime-only gap): because both the
runtime memo (`plan_for_act_turn_rewind` truncates `committed_steps ≤ target`) and
the workspace op-content-log are keyed by the SAME `op_seq == CommittedStep.seq`, a
single `target_seq` restores BOTH substrates coherently — memo[≤K] ⊗ tree[≤K].

Real AgentRegistry + StateLog + real git + the genuine PR-1 capture observer (the
`step_completed` append fires it — no mock); only the per-step file write (the op
effect) is simulated, live-fork-gate style.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.snapshot_generations import is_active_seq, rewind
from reyn.core.events.state_log import StateLog
from reyn.skill.skill_resume_coordinator import SkillResumeCoordinator
from reyn.skill.skill_snapshot import SkillSnapshot

_WS_FILE = "code.py"
_needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _make_registry(tmp_path: Path, *, act_turn_capture: bool, workspace_capture: bool = True) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
        workspace_capture=workspace_capture,
        act_turn_capture=act_turn_capture,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


async def _step(reg: AgentRegistry, tmp_path: Path, *, content: str, oid: str, run_id: str = "r1") -> int:
    """Simulate an op: write the workspace file, then append step_completed (which
    fires the genuine PR-1 capture observer → write-tree into the op-content-log)."""
    (tmp_path / _WS_FILE).write_text(content, encoding="utf-8")
    return await reg.state_log.append(
        "step_completed", target="alpha", run_id=run_id, phase="p",
        op_kind="file", op_invocation_id=oid, args_hash=f"h{oid}", result={"ok": True},
    )


def _file(tmp_path: Path) -> str:
    return (tmp_path / _WS_FILE).read_text(encoding="utf-8")


# ── core: workspace tree[≤K] coherent with memo[≤K] at one target_seq ─────────


@_needs_git
@pytest.mark.asyncio
async def test_act_turn_restore_coherent_with_memo_truncation(tmp_path):
    """Tier 2: restore to step K reverts the workspace to tree[≤K], coherent with
    the runtime memo[≤K] (plan_for_act_turn_rewind) — same target_seq, both substrates."""
    reg = _make_registry(tmp_path, act_turn_capture=True)
    k1 = await _step(reg, tmp_path, content="v1", oid="o1")
    k2 = await _step(reg, tmp_path, content="v2", oid="o2")
    k3 = await _step(reg, tmp_path, content="v3", oid="o3")
    assert _file(tmp_path) == "v3"                          # head

    # workspace half: restore to act-turn step K2.
    await reg.restore_workspace_to_act_turn(k2)
    assert _file(tmp_path) == "v2"                          # tree[≤K2]

    # runtime half: the SAME target_seq truncates the memo to ≤ K2 (k3 dropped).
    snap = SkillSnapshot.empty("r1", "demo", {})
    wal = [e for e in reg.state_log.iter_from(1) if e.get("run_id") == "r1"]
    plan = SkillResumeCoordinator().plan_for_act_turn_rewind(
        snapshot=snap, wal_events=wal, target_seq=k2,
    )
    memo_seqs = sorted(c.seq for c in plan.committed_steps)
    assert memo_seqs == [k1, k2]                            # memo[≤K2] — coherent with tree[≤K2]


# ── lineage: is_active-honoring (abandoned op-trees skipped) ──────────────────


@_needs_git
@pytest.mark.asyncio
async def test_act_turn_restore_skips_abandoned_optree(tmp_path):
    """Tier 2: an op-tree on an abandoned interval is skipped (is_active filter).

    Capture k1(v1), k2(v2); rewind to k1 (abandons k2); capture k_post(v3) on the
    new active branch. Restoring at-or-below the head must pick the ACTIVE k_post
    (v3), NOT the abandoned k2 (v2) — even though k2 < k_post."""
    reg = _make_registry(tmp_path, act_turn_capture=True)
    k1 = await _step(reg, tmp_path, content="v1", oid="o1")
    k2 = await _step(reg, tmp_path, content="v2", oid="o2")
    await rewind(reg.state_log, target_n=k1)               # abandons (k1, R) ∋ k2
    assert not is_active_seq(reg.state_log, k2)             # k2 now abandoned
    k_post = await _step(reg, tmp_path, content="v3", oid="o3")
    assert is_active_seq(reg.state_log, k_post)

    await reg.restore_workspace_to_act_turn(k_post)
    assert _file(tmp_path) == "v3"                          # active k_post, NOT abandoned k2's v2


# ── boundary fallback + no-op ─────────────────────────────────────────────────


@_needs_git
@pytest.mark.asyncio
async def test_act_turn_restore_falls_back_when_no_optree_below_target(tmp_path):
    """Tier 2: no act-turn op-tree ≤ target → boundary fallback (no op-tree restore)."""
    reg = _make_registry(tmp_path, act_turn_capture=True)
    k1 = await _step(reg, tmp_path, content="v1", oid="o1")
    # target below the first op-tree → no op-tree ≤ target → fallback path (no
    # boundary generation captured here either → no-op, returns None, no crash).
    out = await reg.restore_workspace_to_act_turn(k1 - 1)
    assert out is None


@pytest.mark.asyncio
async def test_act_turn_restore_noop_when_capture_off(tmp_path):
    """Tier 2: act_turn_capture=False → no op-content-log → restore is a no-op."""
    reg = _make_registry(tmp_path, act_turn_capture=False)
    assert reg.op_content_log is None
    assert await reg.restore_workspace_to_act_turn(5) is None
