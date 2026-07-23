"""Tier 2: #2103 C2 — fail-closed cap-walk over topology profile bindings (gate-6 + gate-2).

gate-6 (generalising the #2161 lineage fix to the topology-profile path): a DECLARED
topology capability_profile binding whose profile file is ABSENT or MALFORMED must FAIL
CLOSED — compose the restrictive _delegate floor, NOT silently skip. Skipping is the
fail-OPEN escalation (delete/corrupt the profile → the declared narrowing vanishes → the
member resolves WIDER than intended). The discriminator vs a benign skip is EXISTENCE: a
binding declared-but-unresolvable fails closed; NO binding declared stays unrestricted
(present-but-unrestricted, byte-identical — the analog of #2161's present-but-None skip).

gate-2 (can_send member-restriction): a topology's permit() edges are confined to its
members; since C1 restricts an LLM-created topology's members to the creator's spawn
subtree, the edges are confined to the subtree by construction — no broader reach-check
(lead Q2 steer). Confirmed here.

The cascade live-prune of a purged member's profiles[member] (sandbox_2's #2162) is
verified by tests/test_cascade_profile_preserve_2103.py (referenced, not duplicated).

Real AgentRegistry + StateLog + on-disk topology/profile YAML + RouterHostAdapter (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import tool_contextually_denied
from tests._support.router_host_adapter import make_adapter


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log
    )


def _bind(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    """Write a topology binding `member`→`profile` and the profile YAML (on disk, no mock)."""
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{member}.yaml").write_text(
        f"name: {member}\nkind: network\nmembers: [{member}, peer]\nprofiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


def _profile_path(tmp_path: Path, profile: str) -> Path:
    return tmp_path / ".reyn" / "capability_profiles" / f"{profile}.yaml"


# ── gate-6: declared-but-unresolvable binding → fail closed ─────────────────────────


@pytest.mark.asyncio
async def test_missing_bound_profile_fails_closed(tmp_path):
    """Tier 2: (LOAD-BEARING falsification) a member bound to a profile whose file is then
    DELETED (purge / typo) still resolves CAPPED (the _delegate floor), NOT widened to
    unrestricted. RED if the missing-file branch skips (continue) instead of composing the
    floor — the skip is the delete-to-uncap fail-open escalation #2161 closed for lineage."""
    _bind(tmp_path, member="m", profile="narrow",
          body="name: narrow\ntool_deny: [marker_tool]\n")
    reg = _registry(tmp_path)
    reg.create("m")
    reg.create("peer")

    # control: with the profile present, the declared narrowing resolves
    c0, _ = reg.resolved_profile_for("m")
    assert c0 is not None
    assert tool_contextually_denied(c0, "marker_tool")

    # purge the bound profile file → the declared narrowing is now unresolvable
    _profile_path(tmp_path, "narrow").unlink()

    # FAIL CLOSED: still capped (floor), NOT None/unrestricted
    c1, _ = reg.resolved_profile_for("m")
    assert c1 is not None  # a skip would yield None (no layer) = unrestricted = ESCALATION
    assert tool_contextually_denied(c1, "exec")  # a floored ("spawn"-peer) class
    assert tool_contextually_denied(c1, "agent_spawn")     # the spawn class, floored


@pytest.mark.asyncio
async def test_malformed_bound_profile_fails_closed(tmp_path):
    """Tier 2: a declared binding whose profile file is PRESENT but MALFORMED is likewise
    unresolvable → FAIL CLOSED (floor), not skip. RED if the malformed branch skips."""
    _bind(tmp_path, member="m", profile="broken", body="name: broken\ntool_deny: [marker_tool]\n")
    reg = _registry(tmp_path)
    reg.create("m")
    reg.create("peer")
    # corrupt the YAML (a mapping value that won't parse into a profile)
    _profile_path(tmp_path, "broken").write_text(": : not valid yaml : :\n", encoding="utf-8")

    c, _ = reg.resolved_profile_for("m")
    assert c is not None  # fail-closed, not skipped-to-None
    assert tool_contextually_denied(c, "exec")


@pytest.mark.asyncio
async def test_no_binding_declared_stays_unrestricted(tmp_path):
    """Tier 2: (the discriminator) a member with NO topology binding declared resolves
    UNRESTRICTED (None, byte-identical to pre-#1827) — fail-closed must fire ONLY on a
    declared-but-unresolvable binding, never on the benign no-binding case. RED if gate-6
    over-broadly floors an unbound member (the over-restriction failure mode)."""
    reg = _registry(tmp_path)
    reg.create("solo")

    contextual, excluded = reg.resolved_profile_for("solo")
    assert contextual is None and excluded == frozenset()


# ── gate-2: edges confined to (subtree) members ─────────────────────────────────────


@pytest.mark.asyncio
async def test_topology_create_edges_confined_to_subtree_members(tmp_path):
    """Tier 2: a topology_create'd network permits messaging only AMONG its members — and
    since C1 restricts members to the creator's spawn subtree, the edges are confined to
    the subtree by construction (gate-2, no broader reach-check). An agent outside the
    topology shares no edge with a member."""
    reg = _registry(tmp_path)
    reg.create("coord")
    await reg.create_agent("worker", parent="coord")
    reg.create("outsider")  # operator-top, not wired by coord
    adapter = make_adapter(agent_name="coord", agent_registry=reg)

    res = await adapter.create_topology(name="org", kind="network", members=["coord", "worker"])
    assert res["status"] == "created"

    assert reg.permit("coord", "worker")        # edge within the (subtree) members
    assert not reg.permit("coord", "outsider")  # no edge to a non-member outside the topology
