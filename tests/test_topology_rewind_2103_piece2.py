"""Tier 2: OS invariant — #2103 Piece-2 topology-lifecycle rewind-reconstruction.

Topologies become rewind-durable: every state-affecting mutation routes through a
logged seam (create_topology / add_topology_member / remove_topology_member /
delete_topology + the agent-purge cascade) that emits a topology_created /
topology_updated / topology_removed WAL event. _materialize_rewind then reconstructs
the topology config-set AS-OF-CUT from those WAL events (latest-≤-cut-wins, full
config per event) — sourced from the WAL only, never the rotated audit log.

MUST-2 (scoping): reconstruction touches ONLY WAL-tracked topology names — a
pre-WAL / untracked topology is never created, mutated, or deleted by rewind.
MUST-1 (emit completeness): the agent-purge cascade routes through the same logged
seam, so a sync cascade on a tracked topology cannot diverge on reconstruction.

Real AgentRegistry + StateLog + on-disk topologies (no mocks). _materialize_rewind
is driven directly for precise as-of-cut control (the path rewind_to + crash
recovery share).
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
    """Tier 2: a topology CREATED after the rewind cut does not exist as-of-cut —
    reconstruction removes both its on-disk YAML and its in-memory entry."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    await log.append("inbox_put", target="a", msg_id="x", msg_kind="user",
                     payload={"text": "x"})                  # seq 1 (non-lifecycle)
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 2

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)

    assert not reg.topology_exists("net")
    assert not (tmp_path / ".reyn" / "topologies" / "net.yaml").exists()


@pytest.mark.asyncio
async def test_topology_update_after_cut_is_reverted(tmp_path):
    """Tier 2: a member ADDED after the cut is reverted — the topology is restored to
    its as-of-cut config (the latest topology_* event with seq ≤ cut wins)."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b", "c")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 1
    cut = log.current_seq
    await reg.add_topology_member("net", "c")                # seq 2 (after cut)

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=cut)

    assert reg.topology_exists("net")
    assert set(reg.get_topology("net").members) == {"a", "b"}  # c reverted


@pytest.mark.asyncio
async def test_topology_removed_after_cut_is_restored(tmp_path):
    """Tier 2: a topology DELETED after the cut is RESTORED with its as-of-cut config —
    the create event (≤ cut) wins over the later topology_removed (> cut)."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    log = reg.state_log
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))  # seq 1
    cut = log.current_seq
    await reg.delete_topology("net")                         # seq 2 (after cut)
    assert not reg.topology_exists("net")                    # gone live

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=cut)

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
    await log.append("inbox_put", target="a", msg_id="x", msg_kind="user",
                     payload={"text": "x"})                  # seq 1
    await reg.create_topology(Topology.new("tracked", kind="network", members=["a", "b"]))  # seq 2

    # cut before the tracked create → tracked removed, untracked survives.
    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)

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
    await reg.archive_agent("a", purge=True)                 # cascade shrinks "net"

    emitted = [
        e for e in log.iter_from(before + 1)
        if e.get("kind") in ("topology_updated", "topology_removed")
    ]
    assert emitted, "purge cascade must emit a topology-lifecycle WAL event (MUST-1)"
