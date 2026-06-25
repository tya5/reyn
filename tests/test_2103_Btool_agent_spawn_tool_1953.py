"""Tier 2: #2103 B-tool — the agent_spawn surface (reachability + floor + lineage cap).

agent_spawn is the LLM org-design primitive: create a new agent under the spawner. The
3-seam wiring (register → advertise → dispatch) must reach the REAL gate (the #2120
advertised-but-not-dispatched lesson), the #2081 floor must default-deny it (spawning is
restrict-floored), and the create-via-spawn path must set the OS lineage so the new agent
is capped at ⊆ the spawner — recursively (grandchild ⊆ child ⊆ parent).

Real AgentRegistry + StateLog + on-disk topology/profile YAML (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import tool_contextually_denied


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(project_root=tmp_path, session_factory=lambda p: None, state_log=state_log)


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


def test_agent_spawn_is_dispatch_routed_and_advertised():
    """Tier 2: agent_spawn reaches the REAL gate — it is in RouterLoop.REGISTRY_DISPATCH_TOOLS
    (dispatch) and registered router=allow (advertise via build_tools, pinned by
    test_router_tools' EXPECTED_TOOL_NAMES). Guards the #2120 advertised-but-not-dispatched
    class (the LLM would hit 'unhandled tool: agent_spawn')."""
    from reyn.runtime.router_loop import RouterLoop
    assert "agent_spawn" in RouterLoop.REGISTRY_DISPATCH_TOOLS


def test_agent_spawn_is_floored_default_deny():
    """Tier 2: #2081 — agent_spawn is in the _delegate floor (spawning is restrict-floored,
    default-deny, re-grantable within parent bounds), AND declared bare-only in the #2111
    SoT (router-only, no qualified alias). RED if the floor entry is dropped."""
    from reyn.security.permissions.capability_profile import (
        _FLOORED_BARE_ONLY,
        builtin_delegate_profile,
    )
    assert "agent_spawn" in builtin_delegate_profile().tool_deny  # floored
    assert "agent_spawn" in _FLOORED_BARE_ONLY                    # alias-SoT bare-only


@pytest.mark.asyncio
async def test_create_via_spawn_caps_child_at_parent(tmp_path):
    """Tier 2: create_agent(parent=P) records the OS-set lineage → the new agent resolves
    ⊆ P (the spawner's deny propagates). RED if the lineage isn't recorded on the
    create-via-spawn path."""
    _bind(tmp_path, member="parent", profile="prole", body="name: prole\ntool_deny: [sandboxed_exec]\n")
    reg = _registry(tmp_path)
    reg.create("parent")                              # the spawner pre-exists
    await reg.create_agent("child", parent="parent")  # create-via-spawn: lineage set
    contextual, _ = reg.resolved_profile_for("child")
    assert contextual is not None
    assert tool_contextually_denied(contextual, "sandboxed_exec")  # capped at parent


@pytest.mark.asyncio
async def test_grandchild_subseteq_child_subseteq_parent(tmp_path):
    """Tier 2: the cap is RECURSIVE over a real spawned chain P→C→GC — the grandchild
    denies BOTH the parent's deny (via the child) AND the child's own deny (GC ⊆ C ⊆ P).
    B-core close-review note 2. RED if the recursion is absent (GC would miss P's deny)."""
    _bind(tmp_path, member="parent", profile="prole", body="name: prole\ntool_deny: [exec_p]\n")
    _bind(tmp_path, member="child", profile="crole", body="name: crole\ntool_deny: [exec_c]\n")
    reg = _registry(tmp_path)
    reg.create("parent")
    await reg.create_agent("child", parent="parent")      # child ⊆ parent
    await reg.create_agent("gchild", parent="child")      # gchild ⊆ child (⊆ parent)

    contextual, _ = reg.resolved_profile_for("gchild")
    assert contextual is not None
    assert tool_contextually_denied(contextual, "exec_c")  # the child's own deny
    assert tool_contextually_denied(contextual, "exec_p")  # the PARENT's deny, via the child (recursive)
