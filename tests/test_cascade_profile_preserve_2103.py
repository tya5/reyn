"""Tier 2: OS invariant — #2103 cascade preserves surviving members' cap-profiles.

_cascade_agent_removal (fired when an agent is purged) drops the purged agent from
every topology it's a member of. It rebuilt the shrunk topology WITHOUT profiles →
it wiped EVERY capability_profile binding, so purging one member silently changed a
SURVIVOR's effective capability: resolved_profile_for treats a missing binding as
no-narrowing (full ⊆-parent cap), so a survivor's narrowing binding being dropped is
a widen/escalation. The cascade must drop ONLY the removed member's binding and
preserve the survivors'.

Real AgentRegistry + StateLog + on-disk topologies (no mocks); assertions on the
public get_topology surface.
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
async def test_purge_preserves_survivor_profile_drops_removed(tmp_path):
    """Tier 2: purging member A drops A's capability_profile binding but PRESERVES
    survivor B's — so an unrelated-member purge does not silently widen B's effective
    capability (the escalation guard). RED with the old wipe-all-profiles rebuild."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b", "c")
    await reg.create_topology(
        Topology.new("net", kind="network", members=["a", "b", "c"],
                     profiles={"a": "pa", "b": "pb"}),
    )

    await reg.archive_agent("a", purge=True)   # cascade shrinks "net" → [b, c]

    topo = reg.get_topology("net")
    assert set(topo.members) == {"b", "c"}
    assert topo.profiles == {"b": "pb"}        # a dropped, b PRESERVED (not wiped)


@pytest.mark.asyncio
async def test_purge_without_profiles_is_noop_on_profiles(tmp_path):
    """Tier 2: the no-binding case is unaffected — a topology with no cap-profiles
    shrinks normally on a member purge (regression-free for the common case)."""
    reg = _make_registry(tmp_path)
    _seed_agents(tmp_path, "a", "b")
    await reg.create_topology(Topology.new("net", kind="network", members=["a", "b"]))

    await reg.archive_agent("a", purge=True)   # shrinks "net" → [b]

    topo = reg.get_topology("net")
    assert set(topo.members) == {"b"}
    assert topo.profiles == {}                 # no bindings to begin with → still none
