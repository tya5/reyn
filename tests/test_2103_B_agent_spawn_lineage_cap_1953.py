"""Tier 2: #2103 B — a spawned agent's capability is capped at ⊆ its PARENT (Decision A).

agent-spawn records an OS-set, immutable spawn lineage (child → parent). resolved_profile_for
composes the parent's LIVE resolved effective as one more restrict-only conjunct
(compose_resolved is a lattice-meet: ∩ allow, ∪ deny — order-independent), so a spawned
agent can NEVER exceed its parent — even with a mis-specified wider subset or a topology
re-grant. The cap is LIVE (re-resolved each time), which is what makes the topology
re-grant safe and is why Decision A (compose-parent) is REQUIRED over a persisted ⊆
snapshot (which would go stale).

The 4 falsifications (no mocks; real AgentRegistry + on-disk topology/profile YAML):
  (a) wider subset → child still capped at parent (parent's deny propagates).
  (b) DISCRIMINATOR — narrow the parent AFTER spawn → the child re-caps LIVE (GREEN
      under A; RED under a persisted-⊆ snapshot, which is the whole point).
  (c) a topology re-grant of a parent-denied tool is capped at the parent (∪-deny wins).
  (d) the lineage is OS-set + immutable (a re-set to a different parent is refused) —
      the forge-guard linchpin.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import ContextualPermission, tool_contextually_denied


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(project_root=tmp_path, session_factory=lambda profile: None)


def _bind(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    """Bind ``member`` to a capability ``profile`` via a topology + write the profile YAML."""
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / f"{member}.yaml").write_text(
        f"name: {member}\nkind: network\nmembers: [{member}, peer]\n"
        f"profiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


def test_a_wider_subset_is_capped_at_parent(tmp_path: Path) -> None:
    """Tier 2: (a) the parent denies sandboxed_exec; the child has NO own deny — yet the child
    is capped at ⊆ parent via the live parent-conjunct, so it denies sandboxed_exec too.
    RED if the parent-conjunct is absent (the child would not inherit the parent's deny)."""
    _bind(tmp_path, member="P", profile="prole", body="name: prole\ntool_deny: [sandboxed_exec]\n")
    reg = _registry(tmp_path)
    reg._record_spawn_lineage("C", "P")

    contextual, _ = reg.resolved_profile_for("C")
    assert isinstance(contextual, ContextualPermission)
    assert tool_contextually_denied(contextual, "sandboxed_exec")  # capped at parent


def test_b_narrow_parent_after_spawn_recaps_live(tmp_path: Path) -> None:
    """Tier 2: (b) DISCRIMINATOR — narrowing the PARENT after the spawn re-caps the child LIVE.
    GREEN under Decision A (the parent is re-resolved on each child resolve); a
    persisted-⊆ snapshot would NOT reflect the later narrowing → RED. This is the test
    that distinguishes A from B (without it the suite passes under both)."""
    _bind(tmp_path, member="P", profile="prole", body="name: prole\ntool_deny: [exec_x]\n")
    reg = _registry(tmp_path)
    reg._record_spawn_lineage("C", "P")
    first, _ = reg.resolved_profile_for("C")
    assert tool_contextually_denied(first, "exec_x")
    assert not tool_contextually_denied(first, "exec_y")  # not yet denied

    # the parent is narrowed FURTHER, after the spawn.
    _bind(tmp_path, member="P", profile="prole",
          body="name: prole\ntool_deny: [exec_x, exec_y]\n")
    after, _ = reg.resolved_profile_for("C")
    assert tool_contextually_denied(after, "exec_x")
    assert tool_contextually_denied(after, "exec_y")  # LIVE re-cap (RED under a stale snapshot)


def test_c_topology_regrant_is_capped_at_parent(tmp_path: Path) -> None:
    """Tier 2: (c) a topology binding for the CHILD that allow-lists a parent-denied tool does
    NOT re-grant it — the parent-conjunct's ∪-deny wins. A re-grant is bounded ONLY
    because the live parent-conjunct caps it."""
    _bind(tmp_path, member="P", profile="prole", body="name: prole\ntool_deny: [sandboxed_exec]\n")
    # the child is bound to a profile that tries to ALLOW sandboxed_exec (a re-grant attempt).
    _bind(tmp_path, member="C", profile="crole", body="name: crole\ntool_allow: [sandboxed_exec, read_file]\n")
    reg = _registry(tmp_path)
    reg._record_spawn_lineage("C", "P")

    contextual, _ = reg.resolved_profile_for("C")
    assert tool_contextually_denied(contextual, "sandboxed_exec")  # capped — re-grant refused


def test_d_lineage_is_os_set_and_immutable(tmp_path: Path) -> None:
    """Tier 2: (d) the lineage is the no-escalation linchpin, so it is set-once + immutable —
    a re-set to a DIFFERENT parent is refused (the forge-guard). Idempotent on the same
    parent (rewind-reconstruction may replay)."""
    import pytest
    reg = _registry(tmp_path)
    reg._record_spawn_lineage("C", "P")
    reg._record_spawn_lineage("C", "P")  # idempotent (same parent) — no error
    with pytest.raises(ValueError):
        reg._record_spawn_lineage("C", "EVIL")  # re-parent to escalate → refused
    with pytest.raises(ValueError):
        reg._record_spawn_lineage("X", "X")      # self-link → refused


def test_unspawned_agent_has_no_parent_cap(tmp_path: Path) -> None:
    """Tier 2: sanity — an agent with NO recorded lineage gets no parent-conjunct (byte-identical
    to pre-#2103-B) — the cap requires the OS-set lineage, not anything the LLM supplies."""
    reg = _registry(tmp_path)
    assert reg.resolved_profile_for("solo") == (None, frozenset())
