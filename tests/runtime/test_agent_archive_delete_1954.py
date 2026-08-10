"""Tier 2: OS invariant — #1954 agent archive-default delete preserves rewind.

The bug: `AgentRegistry.remove` hard-`rmtree`'d `.reyn/agents/<name>/`, destroying
the runtime PITR generations the rewind materialiser reconstructs from → you could
not time-travel to before an agent-delete. Option A (owner-approved): delete
ARCHIVES by default (generations kept in place, tombstone marker) so rewind works
within the retention window; an explicit `purge=True` is the guarded hard-delete.

Real `AgentRegistry` + `StateLog` + on-disk agents (no mocks). The headline test
asserts against the REAL rewind path (`rewind_to` → `_materialize_rewind`), not a
proxy: an archived agent is still reconstructed as-of the rewind target.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
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


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


def _inbox_ids(snap: AgentSnapshot) -> list[str]:
    return [m["id"] for m in snap.inbox]


@pytest.mark.asyncio
async def test_archive_delete_keeps_rewind_to_before_delete_working(tmp_path):
    """Tier 2: after an archive-delete, rewind-to-before-the-delete STILL
    reconstructs the deleted agent's pre-delete state (the headline bug-fix).

    Asserted against the real ``rewind_to`` → ``_materialize_rewind`` path: the
    archived agent appears in the reconstructed set and its snapshot reflects its
    <=target work — which the old hard-rmtree made impossible (generations gone).
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    _seed_agent(tmp_path, "survivor")
    log = reg.state_log
    await _put(log, "victim", "v1")        # seq 1 (<=1, kept)
    await _put(log, "survivor", "s1")      # seq 2
    await _put(log, "victim", "v2")        # seq 3

    # Archive-delete the agent (default = soft-delete, generations kept).
    reg.remove("victim")

    # Rewind the whole world to seq 1 — the deleted agent must come back.
    result = await reg.rewind_to(1)

    assert "victim" in result["agents"]    # reconstructed despite the delete
    victim = AgentSnapshot.load("victim", _snap_path(tmp_path, "victim"))
    assert _inbox_ids(victim) == ["v1"]    # pre-delete state recovered (v2 cut)


@pytest.mark.asyncio
async def test_purge_hard_deletes_and_removes_from_rewind(tmp_path):
    """Tier 2: the guarded escape hatch ``remove(purge=True)`` is a real
    hard-delete — the agent dir is gone and it is NOT reconstructed by a rewind
    to before the delete (contrast with the archive default)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    _seed_agent(tmp_path, "survivor")
    log = reg.state_log
    await _put(log, "victim", "v1")
    await _put(log, "survivor", "s1")

    reg.remove("victim", purge=True)

    assert not (tmp_path / ".reyn" / "agents" / "victim").exists()
    result = await reg.rewind_to(1)
    assert "victim" not in result["agents"]   # hard-deleted → gone from rewind
    assert "survivor" in result["agents"]


@pytest.mark.asyncio
async def test_archive_hides_from_active_listing_but_kept_on_disk(tmp_path):
    """Tier 2: an archived agent is hidden from the active listing
    (``list_active_names``) yet remains on disk + in the all-inclusive
    ``list_names`` (so the rewind/GC substrate still reaches it)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "victim")

    reg.remove("victim")

    assert reg.is_archived("victim")
    assert "victim" in reg.list_names()              # substrate still sees it
    assert "victim" not in reg.list_active_names()   # active surfaces hide it
    assert "alpha" in reg.list_active_names()
    assert (tmp_path / ".reyn" / "agents" / "victim").is_dir()  # kept on disk


@pytest.mark.asyncio
async def test_archived_agent_auto_purged_once_floor_passes_archival_seq(tmp_path):
    """Tier 2: slice-2 WAL-window GC hard-purges an archived agent once the
    retention floor passes its archival seq (§24 — the soft-delete left the
    window), and retains it while the floor is at-or-below that seq."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    log = reg.state_log
    await _put(log, "victim", "v1")        # seq 1
    await _put(log, "victim", "v2")        # seq 2 -> archival seq = 2

    _seed_agent(tmp_path, "x")
    reg.add_topology(Topology(name="squad", kind="network", members=("victim", "x")))

    reg.remove("victim")                    # archived at current_seq == 2
    victim_dir = tmp_path / ".reyn" / "agents" / "victim"

    # Floor at the archival seq -> still within the window -> retained.
    await reg._prune_generations_below(2)
    assert victim_dir.is_dir()
    assert "victim" in reg.get_topology("squad").members   # membership preserved

    # Floor past the archival seq -> soft-delete left the window -> purged + cascaded.
    await reg._prune_generations_below(3)
    assert not victim_dir.exists()
    assert "victim" not in reg.get_topology("squad").members   # cascaded on purge


@pytest.mark.asyncio
async def test_archive_preserves_topology_membership_and_rewind_restores_org(tmp_path):
    """Tier 2: archive PRESERVES topology membership (only purge cascades), so a
    rewind-to-before-archive restores the agent to its ORG, not just its state —
    topologies live outside the rewound agent dir, so a cascade-on-archive would
    permanently org-orphan the agent even after a rewind."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    _seed_agent(tmp_path, "other")
    reg.add_topology(Topology(name="squad", kind="network", members=("victim", "other")))
    log = reg.state_log
    await _put(log, "victim", "v1")        # seq 1
    await _put(log, "victim", "v2")        # seq 2

    reg.remove("victim")                    # archive (default)
    assert "victim" in reg.get_topology("squad").members   # NOT cascaded

    result = await reg.rewind_to(1)         # rewind to before the archive
    assert "victim" in result["agents"]                    # state reconstructed
    assert "victim" in reg.get_topology("squad").members   # + still in its org


@pytest.mark.asyncio
async def test_archived_member_skipped_from_active_comm(tmp_path):
    """Tier 2: an archived agent is skipped from active comm (``permit`` False
    both directions) even though its topology membership is preserved."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    _seed_agent(tmp_path, "other")
    reg.add_topology(Topology(name="squad", kind="network", members=("victim", "other")))
    assert reg.permit("other", "victim") is True     # live pair can communicate

    reg.remove("victim")                                # archive

    assert "victim" in reg.get_topology("squad").members   # membership preserved
    assert reg.permit("other", "victim") is False        # but skipped (archived)
    assert reg.permit("victim", "other") is False


@pytest.mark.asyncio
async def test_purge_cascades_topology_removal(tmp_path):
    """Tier 2: an explicit purge (hard-delete) cascades the topology removal — the
    agent is dropped from its topology (no dangling reference), unlike archive."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    _seed_agent(tmp_path, "other")
    reg.add_topology(Topology(name="squad", kind="network", members=("victim", "other")))

    reg.remove("victim", purge=True)

    assert "victim" not in reg.get_topology("squad").members
    assert "other" in reg.get_topology("squad").members
