"""Tier 2: OS invariant — lineage-correct turn-checkpoint predecessor (#1533 2c).

The 2c edit flow re-runs an edited turn from the state *before* it: checkout the
turn's predecessor, then submit the edit (a new fork). The predecessor must be
the immediately-prior **TURN** checkpoint along the target's **lineage** (its
branch + ancestor branches back to the fork-point) — two subtleties:

  - **turn-kind only**: `record_plan_step_completed` cuts intra-turn plan-step
    checkpoints; a plan-bearing turn's naive predecessor would be a mid-turn
    plan-step. Edit must return to the prior *turn*, so plan-step/phase are skipped.
  - **lineage, not same-branch-max**: when the target is the FIRST checkpoint on a
    forked branch, its predecessor is on the PARENT branch at the fork-point — a
    same-branch-only max would miss it (the over-include sibling trap). Computed
    from the branch-registry (parent_branch_id + fork_point chain), substrate-side.

First turn (no prior turn) → None → UX disables edit (genesis = (b): no pre-turn-1
workspace capture, so genesis-checkout would be workspace-incoherent).

Real AgentRegistry + StateLog + generation stores (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


async def _turn_cp(reg: AgentRegistry, text: str) -> int:
    """A TURN-kind checkpoint: a WAL entry whose kind maps to 'turn' + a gen."""
    seq = await reg.state_log.append(
        "inbox_consume", target="alpha", msg_id=text, msg_kind="user", payload={},
    )
    return _record_gen(reg, seq)


async def _plan_step_cp(reg: AgentRegistry) -> int:
    """A PLAN-STEP-kind checkpoint (must be skipped by predecessor_turn_checkpoint)."""
    seq = await reg.state_log.append(
        "plan_step_completed", target="alpha", payload={},
    )
    return _record_gen(reg, seq)


def _record_gen(reg: AgentRegistry, seq: int) -> int:
    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = seq
    reg._store_for("alpha").record(snap)
    return seq


# ── turn-kind filter ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_predecessor_skips_plan_step_to_prior_turn(tmp_path):
    """Tier 2: a plan-step checkpoint between two turns is skipped — predecessor = prior TURN."""
    reg = _make_registry(tmp_path)
    t1 = await _turn_cp(reg, "turn1")
    await _plan_step_cp(reg)                 # intra-turn plan-step (must be skipped)
    t2 = await _turn_cp(reg, "turn2")

    assert reg.predecessor_turn_checkpoint(t2) == t1   # NOT the plan-step between them


@pytest.mark.asyncio
async def test_predecessor_linear_prior_turn(tmp_path):
    """Tier 2: linear history — predecessor is the immediately-prior turn checkpoint."""
    reg = _make_registry(tmp_path)
    t1 = await _turn_cp(reg, "t1")
    t2 = await _turn_cp(reg, "t2")
    t3 = await _turn_cp(reg, "t3")

    assert reg.predecessor_turn_checkpoint(t3) == t2
    assert reg.predecessor_turn_checkpoint(t2) == t1


# ── first turn → None (genesis = (b)) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_turn_has_no_predecessor(tmp_path):
    """Tier 2: the first turn has no prior turn → None (UX disables first-turn edit)."""
    reg = _make_registry(tmp_path)
    t1 = await _turn_cp(reg, "only")
    assert reg.predecessor_turn_checkpoint(t1) is None


@pytest.mark.asyncio
async def test_first_turn_none_even_with_leading_plan_step(tmp_path):
    """Tier 2: a plan-step before the first turn is not a turn predecessor → still None."""
    reg = _make_registry(tmp_path)
    await _plan_step_cp(reg)                  # not a turn
    t1 = await _turn_cp(reg, "first-turn")
    assert reg.predecessor_turn_checkpoint(t1) is None


# ── lineage: cross-fork-point ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_fork_point_predecessor_is_parent_fork_point_turn(tmp_path):
    """Tier 2: a dead-branch's first turn → predecessor is the PARENT branch's fork-point turn.

    Active turns t1,t2,t3; rewind to t2 (abandons t3's branch — t3 becomes a dead
    branch forked at t2). predecessor_turn_checkpoint(t3) must walk the lineage to
    the parent and return t2 (the fork-point turn), NOT a same-branch-only None.
    """
    reg = _make_registry(tmp_path)
    t1 = await _turn_cp(reg, "t1")
    t2 = await _turn_cp(reg, "t2")
    t3 = await _turn_cp(reg, "t3")           # will be abandoned by the rewind to t2
    await rewind(reg.state_log, target_n=t2)  # t3 now on a dead branch forked at t2

    # t3 is the first (only) turn on the dead branch → predecessor = parent fork-point turn t2.
    assert reg.predecessor_turn_checkpoint(t3) == t2
    # sanity: t2's own predecessor is still t1 (active lineage).
    assert reg.predecessor_turn_checkpoint(t2) == t1


@pytest.mark.asyncio
async def test_empty_or_no_state_log_returns_none(tmp_path):
    """Tier 2: no checkpoints → None (slot-in-unconditionally for the UX gate)."""
    reg = _make_registry(tmp_path)
    assert reg.predecessor_turn_checkpoint(5) is None
