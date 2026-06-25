"""Tier 2: #2103 C1 — the topology_create surface (reachability + floor + subtree forge-guard).

topology_create is the LLM org-WIRING primitive: group agents you spawned into a topology
and optionally bind capability_profiles. The 3-seam wiring (register → advertise →
dispatch) must reach the REAL gate (the #2120 advertised-but-not-dispatched lesson); the
#2081 floor must default-deny it (org-design is restrict-floored like spawn); the
create-via-tool path must route through the logged create_topology emit seam (#2153,
rewind durability); and — the load-bearing C1 safety property (lead-approved Q1) — the
host seam must REJECT any member not in the creator's spawn subtree. That subtree
restriction is what makes the profile bindings ⊆-creator BY CONSTRUCTION (each member is
already capped ⊆ the creator via the B-core lineage conjunct), and it is the reason the
gate-6 fail-open is safely deferrable to C2 for LLM-bindable members — so it must be
airtight here.

Real AgentRegistry + StateLog + RouterHostAdapter (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from tests._support.router_host_adapter import make_adapter


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log
    )


# ── reachability + floor (the 3-seam + #2081/#2111 invariants) ──────────────────────


def test_topology_create_is_dispatch_routed():
    """Tier 2: topology_create reaches the REAL gate — it is in
    RouterLoop.REGISTRY_DISPATCH_TOOLS (dispatch). Guards the #2120
    advertised-but-not-dispatched class (the LLM would hit 'unhandled tool')."""
    from reyn.runtime.router_loop import RouterLoop
    assert "topology_create" in RouterLoop.REGISTRY_DISPATCH_TOOLS


def test_topology_create_is_advertised_router_allow():
    """Tier 2: topology_create is registered router=allow + advertised via build_tools, so
    the LLM can actually see it (the #2120 advertise-drift lesson)."""
    from reyn.tools import get_default_registry
    d = get_default_registry().lookup("topology_create")
    assert d is not None and d.gates.router == "allow" and d.gates.phase == "deny"


def test_topology_create_is_floored_default_deny():
    """Tier 2: #2081 — topology_create is in the _delegate floor (org-design is
    restrict-floored, default-deny), AND declared bare-only in the #2111 SoT (router-only,
    no qualified alias). RED if either floor entry is dropped."""
    from reyn.security.permissions.capability_profile import (
        _FLOORED_BARE_ONLY,
        builtin_delegate_profile,
    )
    assert "topology_create" in builtin_delegate_profile().tool_deny  # floored
    assert "topology_create" in _FLOORED_BARE_ONLY                    # alias-SoT bare-only


# ── is_spawn_descendant: the subtree predicate backbone ─────────────────────────────


@pytest.mark.asyncio
async def test_is_spawn_descendant_predicate(tmp_path):
    """Tier 2: the subtree predicate — an agent is in P's subtree iff it is P itself or a
    transitive spawn-descendant; an unrelated agent / a no-lineage operator-top agent is
    NOT. This is the backbone of the C1 forge-guard."""
    reg = _registry(tmp_path)
    reg.create("root")
    await reg.create_agent("child", parent="root")
    await reg.create_agent("grandchild", parent="child")
    reg.create("stranger")  # operator-top, no lineage edge

    assert reg.is_spawn_descendant("root", "root")          # self
    assert reg.is_spawn_descendant("child", "root")         # direct child
    assert reg.is_spawn_descendant("grandchild", "root")    # transitive (recursive)
    assert not reg.is_spawn_descendant("stranger", "root")  # unrelated peer
    assert not reg.is_spawn_descendant("root", "child")     # ancestor is NOT a descendant
    assert not reg.is_spawn_descendant("nonexistent", "root")  # no lineage at all


# ── the host seam: accept in-subtree (+ routing) / reject non-subtree (falsification) ─


@pytest.mark.asyncio
async def test_create_topology_accepts_subtree_members_and_routes_through_emit_seam(tmp_path):
    """Tier 2: a topology over the creator + an agent it spawned is CREATED, and routes
    through the LOGGED create_topology emit seam (#2153) — a topology_created WAL event is
    emitted (rewind durability), not the sync add_topology. The control for the rejection
    falsification below."""
    reg = _registry(tmp_path)
    reg.create("coord")
    await reg.create_agent("worker", parent="coord")
    adapter = make_adapter(agent_name="coord", agent_registry=reg)

    res = await adapter.create_topology(
        name="myteam", kind="network", members=["coord", "worker"],
    )
    assert res["status"] == "created"
    assert reg.get_topology("myteam") is not None  # persisted
    # routed through the emit seam → topology_created landed in the WAL
    events = list(reg._state_log.iter_from(0))
    assert any(e.get("kind") == "topology_created" for e in events)


@pytest.mark.asyncio
async def test_create_topology_rejects_non_subtree_member(tmp_path):
    """Tier 2: (LOAD-BEARING falsification) the host seam REJECTS a topology that wires an
    agent NOT in the creator's spawn subtree — the LLM cannot grant capability to a peer it
    doesn't own. RED if the subtree forge-guard is removed (members ⊆ creator's subtree).
    The whole C1 safety + the gate-6 deferral to C2 rest on this being airtight, so it also
    asserts NO partial side-effect leaked (topology not persisted, no WAL event)."""
    reg = _registry(tmp_path)
    reg.create("coord")
    await reg.create_agent("worker", parent="coord")
    reg.create("outsider")  # exists, but NOT spawned by coord (operator-top peer)
    adapter = make_adapter(agent_name="coord", agent_registry=reg)

    res = await adapter.create_topology(
        name="grab", kind="network", members=["coord", "outsider"],
    )
    assert res["status"] == "error"
    assert res["kind"] == "member_outside_subtree"
    # airtight: the rejection is total — nothing persisted, nothing logged
    import pytest as _pytest
    with _pytest.raises(Exception):
        reg.get_topology("grab")
    events = list(reg._state_log.iter_from(0))
    assert not any(
        e.get("kind") == "topology_created" and e.get("name") == "grab" for e in events
    )


@pytest.mark.asyncio
async def test_create_topology_rejects_non_subtree_member_in_profile_binding(tmp_path):
    """Tier 2: the forge-guard also covers the PROFILE-binding path — a profile that binds a
    non-subtree member is rejected (profiles bind members, so a non-subtree bound member is
    a non-subtree member). RED if the subtree check is bypassed for the profiles map."""
    reg = _registry(tmp_path)
    reg.create("coord")
    await reg.create_agent("worker", parent="coord")
    reg.create("outsider")
    adapter = make_adapter(agent_name="coord", agent_registry=reg)

    res = await adapter.create_topology(
        name="grab2", kind="network",
        members=["coord", "worker", "outsider"],
        profiles={"outsider": "some_profile"},
    )
    assert res["status"] == "error"
    assert res["kind"] == "member_outside_subtree"
