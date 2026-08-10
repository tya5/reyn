"""Tier 2: OS invariant — #2103 Piece-2 topology-lifecycle rewind-reconstruction.

Topologies become rewind-durable: every state-affecting mutation routes through a
logged seam (create_topology / add_topology_member / remove_topology_member /
delete_topology + the agent-purge cascade) that emits a topology_created /
topology_updated / topology_removed WAL event. _materialize_rewind then reconstructs
the topology config-set from those WAL events via ``is_active_seq`` (the same
active-branch predicate as vanish/archive/config) — sourced from the WAL only, never
the rotated audit log.

MUST-2 (scoping): reconstruction touches ONLY WAL-tracked topology names — a
pre-WAL / untracked topology is never created, mutated, or deleted by rewind.
MUST-1 (emit completeness): the agent-purge cascade routes through the same logged
seam, so a sync cascade on a tracked topology cannot diverge on reconstruction.

Real AgentRegistry + StateLog + on-disk topologies (no mocks). _materialize_rewind
is driven directly for precise as-of-cut control (the path rewind_to + crash
recovery share). Each call site adds a rewind record first (production invariant:
both callers guarantee a rewind record exists before _materialize_rewind runs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import rewind
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.topology import Topology


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agents(tmp_path: Path, *names: str) -> None:
    for name in names:
        AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_topology_created_after_cut_is_removed(tmp_path):
    """Tier 2: a topology CREATED on the abandoned branch (N < seq < R) does not exist
    as-of-cut — reconstruction removes both its on-disk YAML and its in-memory entry."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    n_seq = await log.append("inbox_put", target="a", msg_id="x", msg_kind="user",
                             payload={"text": "x"})          # seq 1 = N
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 2 (abandoned)
    R = await rewind(log, target_n=n_seq)                    # seq 3 = R; seq 2 in (1,3)

    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=n_seq)

    assert not reg.topology_exists("net")
    assert not (tmp_path / ".reyn" / "topologies" / "net.yaml").exists()


@pytest.mark.asyncio
async def test_topology_update_after_cut_is_reverted(tmp_path):
    """Tier 2: a member ADDED on the abandoned branch is reverted — the topology is
    restored to its as-of-target-N config (the latest active topology_* event wins)."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b", "c")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 1 = N
    cut = log.current_seq
    await reg.add_topology_member("net", "c")                # seq 2 (abandoned branch)
    R = await rewind(log, target_n=cut)                      # seq 3 = R; seq 2 in (1,3)

    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=cut)

    assert reg.topology_exists("net")
    assert set(reg.get_topology("net").members) == {"a", "b"}  # c reverted


@pytest.mark.asyncio
async def test_topology_removed_after_cut_is_restored(tmp_path):
    """Tier 2: a topology DELETED on the abandoned branch is RESTORED with its
    as-of-target-N config — the active create event wins over the abandoned removal."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 1 = N
    cut = log.current_seq
    await reg.delete_topology("net")                         # seq 2 (abandoned branch)
    assert not reg.topology_exists("net")                    # gone live
    R = await rewind(log, target_n=cut)                      # seq 3 = R; seq 2 in (1,3)

    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=cut)

    assert reg.topology_exists("net")                        # restored
    assert set(reg.get_topology("net").members) == {"a", "b"}


@pytest.mark.asyncio
async def test_untracked_topology_untouched_by_rewind(tmp_path):
    """Tier 2: MUST-2 — a pre-WAL / untracked topology (no lifecycle events) is left
    untouched while a coexisting WAL-tracked topology is reconstructed — falsifies the
    'rewind wipes everything' over-reach. Scoping is to WAL-tracked names only."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    reg.add_topology(Topology.new("legacy", kind="network", members=["a", "b"]))  # NO WAL emit
    n_seq = await log.append("inbox_put", target="a", msg_id="x", msg_kind="user",
                             payload={"text": "x"})          # seq 1 = N
    await reg.create_topology(Topology.new("tracked", kind="network", members=["a", "b"]))  # seq 2 (abandoned)
    R = await rewind(log, target_n=n_seq)                    # seq 3 = R; seq 2 in (1,3)

    # abandoned-branch tracked create → tracked removed; untracked survives (MUST-2).
    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=n_seq)

    assert not reg.topology_exists("tracked")               # tracked: removed
    assert reg.topology_exists("legacy")                    # untracked: untouched
    assert set(reg.get_topology("legacy").members) == {"a", "b"}


@pytest.mark.asyncio
async def test_purge_cascade_emits_topology_event(tmp_path):
    """Tier 2: MUST-1 — purging a topology member routes the cascade mutation through
    the logged seam — a topology-lifecycle WAL event is emitted, so a tracked topology
    cannot diverge on reconstruction via a silent sync cascade."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))

    before = log.current_seq
    await reg.archive_agent("a", purge=True)                 # cascade shrinks "net" → [b]

    emitted = [
        e for e in log.iter_from(before + 1)
        if e.get("kind") in ("topology_updated", "topology_removed")
    ]
    assert emitted, "purge cascade must emit a topology-lifecycle WAL event (MUST-1)"
    # The emitted PAYLOAD must reflect the cascade's actual mutation (shrunk to {b}),
    # not merely that *an* event fired — else reconstruction would diverge.
    ev = emitted[-1]
    assert ev["kind"] == "topology_updated"
    assert set(ev["topology"]["members"]) == {"b"}


@pytest.mark.asyncio
async def test_topology_post_rewind_mutation_applied(tmp_path):
    """Tier 2: topology mutated POST-REWIND (seq > R, active branch) is applied on crash
    recovery — ``is_active_seq=True`` → the post-rewind state wins. Symmetric gap fix
    (#2405): the former ``≤ cut`` excluded post-rewind events, reverting topology to
    as-of-N even when it was legitimately updated on the active branch.

    N=1 (create "net" with [a,b]), R=2 (rewind), seq=3 add member "c" (post-rewind,
    active). Recovery must reflect [a,b,c], not the abandoned/stale [a,b]."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b", "c")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 1 = N
    R = await rewind(log, target_n=log.current_seq)          # seq 2 = R
    await reg.add_topology_member("net", "c")                # seq 3 (post-rewind active)

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)

    assert reg.topology_exists("net")
    assert set(reg.get_topology("net").members) == {"a", "b", "c"}  # post-rewind state


@pytest.mark.asyncio
async def test_topology_abandoned_mutation_undone(tmp_path):
    """Tier 2: topology mutated on the ABANDONED branch (N < seq < R) is undone on crash
    recovery — ``is_active_seq=False`` → abandoned mutation excluded, pre-N state wins.

    N=1 (create "net" with [a,b]), seq=2 add "c" on abandoned branch, R=3 (rewind to
    N). Recovery must reflect [a,b], not the abandoned [a,b,c]."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b", "c")
    log = reg.state_log
    n_seq = await log.append("inbox_put", target="a", msg_id="x", msg_kind="user",
                             payload={"text": "x"})          # seq 1 = N (pre-topology)
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 2 (abandoned create)
    R = await rewind(log, target_n=n_seq)                    # seq 3 = R; seqs (1,3) abandoned

    await reg._materialize_rewind(reconstruct_seq=R, workspace_at_or_below=n_seq)

    assert not reg.topology_exists("net")                    # abandoned create → absent
    assert not (tmp_path / ".reyn" / "topologies" / "net.yaml").exists()
