"""Tier 2: OS invariant — Phase-2 derived branch tree (#1533 2a, ADR-0038 D8).

Real `StateLog` (no mocks). The branch tree + per-checkpoint branch membership are
DERIVED from the reset-record chain via the proven `_abandoned_intervals`
machinery (inherits 1b-1e correctness) — no stored registry, no per-entry
branch_id. Load-bearing case: the over-include repro (an active branch's
`[fork_point, head]` range physically spans its abandoned children, so naive
range-intersection mis-groups; branch_id membership separates them lineage-correctly).
"""
from __future__ import annotations

import pytest

from reyn.core.events.snapshot_generations import (
    ACTIVE_BRANCH_ID,
    branch_ids_for,
    list_branches,
    rewind,
)
from reyn.core.events.state_log import StateLog


async def _put(log: StateLog, text: str) -> int:
    return await log.append(
        "inbox_put", target="a", msg_id=text, msg_kind="user", payload={"text": text},
    )


# ── branch_ids_for: lineage-correct membership (the over-include repro) ────────


@pytest.mark.asyncio
async def test_branch_membership_over_include_repro(tmp_path):
    """Tier 2: rewind-then-continue — abandoned checkpoint is NOT grouped with active.

    The load-bearing case (e2e build-first catch): root 1..10 {checkpoints 3,6,9},
    rewind to 6 (abandons 7-10), continue {12}. The active branch's range [0,13]
    physically CONTAINS the abandoned 9, so a naive range-intersection over-includes
    it. branch_ids_for resolves true membership: 3,6,12 → active(0); 9 → the dead
    branch (the reset-record R that abandoned it).
    """
    log = StateLog(tmp_path / "wal")
    for i in range(1, 11):                      # seq 1..10
        await _put(log, f"m{i}")
    r = await rewind(log, target_n=6)           # seq 11 — abandons (6, 11) = 7..10
    await _put(log, "m12")                      # seq 12
    await _put(log, "m13")                      # seq 13

    ids = branch_ids_for(log, [3, 6, 9, 12])
    assert ids[3] == ACTIVE_BRANCH_ID           # active (<=6, on the live line)
    assert ids[6] == ACTIVE_BRANCH_ID           # the rewind target is active
    assert ids[12] == ACTIVE_BRANCH_ID          # post-rewind continuation = active
    assert ids[9] == r                          # abandoned → the dead branch (id = R)
    assert ids[9] != ACTIVE_BRANCH_ID           # NOT mixed into the active group

    # group-by-branch_id (the UX consumption) yields lineage-correct lists.
    groups: dict[int, list[int]] = {}
    for seq, bid in ids.items():
        groups.setdefault(bid, []).append(seq)
    assert sorted(groups[ACTIVE_BRANCH_ID]) == [3, 6, 12]
    assert groups[r] == [9]


@pytest.mark.asyncio
async def test_no_rewind_all_active(tmp_path):
    """Tier 2: with no rewind, every checkpoint is on the active branch (0)."""
    log = StateLog(tmp_path / "wal")
    for i in range(1, 4):
        await _put(log, f"m{i}")
    assert branch_ids_for(log, [1, 2, 3]) == {1: 0, 2: 0, 3: 0}


# ── list_branches: tree topology ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_branches_single_undo(tmp_path):
    """Tier 2: one rewind → active branch + one dead branch forked at the target."""
    log = StateLog(tmp_path / "wal")
    for i in range(1, 11):
        await _put(log, f"m{i}")
    r = await rewind(log, target_n=6)
    await _put(log, "m12")
    head = log.current_seq

    branches = list_branches(log)
    active = next(b for b in branches if b.is_active)
    dead = [b for b in branches if not b.is_active]
    assert active.branch_id == ACTIVE_BRANCH_ID and active.fork_point_seq == 0
    assert active.head_seq == head and active.parent_branch_id is None
    # exactly one dead branch, and it is the orphaning reset-record R.
    assert {b.branch_id for b in dead} == {r}
    only = dead[0]
    assert only.fork_point_seq == 6        # forked at the rewind target
    assert only.head_seq == r - 1          # top of the rewound-past content
    assert only.parent_branch_id == ACTIVE_BRANCH_ID   # fork point is on the active line


@pytest.mark.asyncio
async def test_list_branches_nested_dead_branches_parent_edges(tmp_path):
    """Tier 2: nested rewinds → dead branches with correct parent nesting.

    a(1) b(2) | rewind→1 (abandons 2) | c(4) d(5) | rewind→4 (abandons 5).
    Two dead branches; the second's fork point (4) is on the active line → parent
    is the active branch (this exercises parent = branch-owning-the-fork-point).
    """
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")                    # seq 1
    await _put(log, "b")                    # seq 2
    r1 = await rewind(log, target_n=1)      # seq 3 — abandons (1,3) = {2}
    await _put(log, "c")                    # seq 4
    await _put(log, "d")                    # seq 5
    r2 = await rewind(log, target_n=4)      # seq 6 — abandons (4,6) = {5}

    branches = list_branches(log)
    by_id = {b.branch_id: b for b in branches}
    assert by_id[ACTIVE_BRANCH_ID].is_active
    assert by_id[r1].fork_point_seq == 1 and by_id[r1].head_seq == r1 - 1
    assert by_id[r2].fork_point_seq == 4 and by_id[r2].head_seq == r2 - 1
    # both fork points (1 and 4) are on the active line → parent = active.
    assert by_id[r1].parent_branch_id == ACTIVE_BRANCH_ID
    assert by_id[r2].parent_branch_id == ACTIVE_BRANCH_ID
    assert not by_id[r1].is_active and not by_id[r2].is_active


@pytest.mark.asyncio
async def test_empty_wal_no_branches(tmp_path):
    """Tier 2: empty WAL → no branches."""
    log = StateLog(tmp_path / "wal")
    assert list_branches(log) == []


@pytest.mark.asyncio
async def test_derivation_robust_without_supersedes(tmp_path):
    """Tier 2: the tree is correct though rewind() never set supersedes (Q2).

    These rewinds omit `supersedes` (default None) — proving the derivation uses
    only always-present (R, target_n) + the interval machinery, not supersedes.
    """
    log = StateLog(tmp_path / "wal")
    for i in range(1, 6):
        await _put(log, f"m{i}")
    r = await rewind(log, target_n=2)       # supersedes NOT passed
    branches = list_branches(log)
    dead = [b for b in branches if not b.is_active]
    assert {b.branch_id for b in dead} == {r}   # exactly the orphaning record
    assert dead[0].fork_point_seq == 2


# ── registry list_rewind_points: branch_id + include_abandoned wiring ─────────


@pytest.mark.asyncio
async def test_list_rewind_points_branch_id_and_include_abandoned(tmp_path):
    """Tier 2: list_rewind_points tags rows with branch_id; include_abandoned gates.

    Wires the 2a→2b contract: default = active-branch checkpoints only (1f); with
    include_abandoned, dead-branch checkpoints appear, each carrying its branch_id
    so the UX groups by it (no range-intersection).
    """
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.core.events.agent_snapshot import AgentSnapshot

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")

    async def put(text):
        return await state_log.append(
            "inbox_put", target="alpha", msg_id=text, msg_kind="user", payload={},
        )

    def record_gen(seq):
        snap = AgentSnapshot.empty("alpha")
        snap.applied_seq = seq
        reg._store_for("alpha").record(snap)

    for i in range(1, 11):                  # seq 1..10
        await put(f"m{i}")
    for s in (3, 6, 9):                      # checkpoints on root
        record_gen(s)
    r = await rewind(state_log, target_n=6)  # seq 11 — abandons 7..10 (incl gen@9)
    await put("m12")                         # seq 12
    record_gen(12)

    # default: active branch only — abandoned gen@9 filtered out (1f behaviour).
    active_rows = reg.list_rewind_points()
    active_seqs = {row["seq"] for row in active_rows}
    assert 9 not in active_seqs
    assert {3, 6, 12} <= active_seqs
    assert all("branch_id" in row for row in active_rows)              # field present
    assert all(row["branch_id"] == ACTIVE_BRANCH_ID for row in active_rows)

    # include_abandoned: dead-branch gen@9 appears, tagged with its branch_id.
    all_rows = reg.list_rewind_points(include_abandoned=True)
    by_seq = {row["seq"]: row for row in all_rows}
    assert 9 in by_seq
    assert by_seq[9]["branch_id"] == r                                 # dead branch
    assert by_seq[3]["branch_id"] == ACTIVE_BRANCH_ID
    assert by_seq[12]["branch_id"] == ACTIVE_BRANCH_ID

    # reg.list_branches(): active + the dead branch.
    branches = reg.list_branches()
    assert any(b.is_active and b.branch_id == ACTIVE_BRANCH_ID for b in branches)
    assert any(b.branch_id == r and not b.is_active and b.fork_point_seq == 6 for b in branches)
