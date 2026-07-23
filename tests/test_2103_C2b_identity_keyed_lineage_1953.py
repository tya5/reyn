"""Tier 2: #2103 C2b — identity-keyed spawn lineage (name-reuse-after-purge, #2166).

`_spawn_lineage` keys the parent edge on a stable IDENTITY (the create_agent-minted
create-seq), not the reusable name. A purged + name-REUSED parent gets a NEW identity, so
an orphan's stored edge (frozen at the OLD identity) reads STALE — defeating BOTH the
name-reuse escalation vectors tui found in the C1 live-verify:

- Probe A (forge-guard bypass): is_spawn_descendant(orphan, reused_name) must be False, so
  the reused-name agent cannot topology_create-wire an agent it never spawned.
- Probe B (capability escalation): resolved_profile_for(orphan) must FAIL CLOSED (the
  stale edge → the _delegate floor), so the orphan does NOT inherit the new same-named
  parent's (wider) capability.

The identity is minted in create_agent — the PRODUCTION spawn seam (CLI / web / slash /
agent_spawn all route through it). A bare ``reg.create()`` (non-spawn operator/test agent)
is the accepted Q2 fallback: no identity → #2161 existence-check governs (no false-positive).

The REWIND-across-reuse half (the destroy-side mirror) + the productionized probes are
tui's lane; this file covers the LIVE registry behaviour. Real AgentRegistry + StateLog
+ RouterHostAdapter (no mocks).
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
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{member}.yaml").write_text(
        f"name: {member}\nkind: network\nmembers: [{member}, peer]\nprofiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


# ── identity assignment ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_mints_distinct_identity_on_name_reuse(tmp_path):
    """Tier 2: create_agent assigns a stable identity; a purged+recreated name gets a
    DIFFERENT identity (the discriminator the staleness check keys on)."""
    reg = _registry(tmp_path)
    await reg.create_agent("coord")
    id1 = reg._agent_create_seq["coord"]
    await reg.archive_agent("coord", purge=True)
    await reg.create_agent("coord")
    id2 = reg._agent_create_seq["coord"]
    assert id1 != id2  # name reused → new identity


# ── probe A: forge-guard bypass (live) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_name_reuse_after_purge_rejects_orphan_forge_guard(tmp_path):
    """Tier 2: (LOAD-BEARING falsification, tui probe A) purge a create_agent'd spawn-parent
    + reuse the name → is_spawn_descendant rejects the orphan (frozen edge identity ≠ the
    reused parent's new identity), so the reused-name agent cannot topology_create-wire the
    orphan it never spawned. RED on the name-keyed lineage (#2166)."""
    reg = _registry(tmp_path)
    await reg.create_agent("coord")                      # operator-top, identity-tracked
    await reg.create_agent("worker", parent="coord")     # edge: worker → (coord, id1)
    assert reg.is_spawn_descendant("worker", "coord")    # control: real descendant

    await reg.archive_agent("coord", purge=True)
    await reg.create_agent("coord")                      # name reused → NEW identity

    assert not reg.is_spawn_descendant("worker", "coord")  # stale edge → not a descendant

    # forge-guard: the reused-name coord cannot wire the orphan it never spawned
    adapter = make_adapter(agent_name="coord", agent_registry=reg)
    res = await adapter.create_topology(
        name="grab", kind="network", members=["coord", "worker"],
    )
    assert res["status"] == "error"
    assert res["kind"] == "member_outside_subtree"


@pytest.mark.asyncio
async def test_no_reuse_preserves_subtree_membership(tmp_path):
    """Tier 2: (boundary control) WITHOUT name-reuse, the live edge identity still matches →
    subtree membership preserved (the fix must not over-reject a genuine descendant)."""
    reg = _registry(tmp_path)
    await reg.create_agent("coord")
    await reg.create_agent("worker", parent="coord")
    assert reg.is_spawn_descendant("worker", "coord")
    adapter = make_adapter(agent_name="coord", agent_registry=reg)
    res = await adapter.create_topology(name="org", kind="network", members=["coord", "worker"])
    assert res["status"] == "created"


# ── probe B: capability escalation (live) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_name_reuse_after_purge_fails_closed_no_cap_escalation(tmp_path):
    """Tier 2: (LOAD-BEARING falsification, tui probe B) an orphan capped ⊆ a purged parent
    does NOT inherit a reused same-named parent's wider capability — the stale edge FAILS
    CLOSED (the _delegate floor). RED on the name-keyed lineage (orphan would resolve under
    the new coord → lose the old parent's deny)."""
    # old coord bound to a profile that denies exec → worker ⊆ coord inherits it
    _bind(tmp_path, member="coord", profile="tight",
          body="name: tight\ntool_deny: [exec]\n")
    reg = _registry(tmp_path)
    await reg.create_agent("coord")
    await reg.create_agent("worker", parent="coord")
    c0, _ = reg.resolved_profile_for("worker")
    assert c0 is not None and tool_contextually_denied(c0, "exec")  # capped ⊆ coord

    await reg.archive_agent("coord", purge=True)
    await reg.create_agent("coord")  # name reused → NEW identity (no tight binding now)

    c1, _ = reg.resolved_profile_for("worker")
    assert c1 is not None  # fail-closed, NOT skipped-to-unrestricted
    assert tool_contextually_denied(c1, "exec")  # STILL denied (floor) — no escalation
