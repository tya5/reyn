"""Tier 2: #2103 S1a — the per-session-config 4th COMBINE layer (capability narrowing).

A spawner can narrow a spawned session's capability via a workspace-backed (P5)
per-session ``config.yaml`` (``.reyn/agents/<name>/state/sessions/<sid>/config.yaml`` —
a capability_profile YAML, sibling of the per-session snapshot). It composes via the
EXISTING #2074 machinery (resolve_profile + compose_resolved ∩) folded into the single
ContextualLayer — NO 4th EffectivePermission conjunct. Restrict-only is structural:
ContextualLayer is one conjunct in ``all(...)``, so the per-session layer can only
narrow within the agent envelope, never re-grant.

S1a is the LAYER only (inert until a spawner writes the config — the session_spawn tool
is S1bc). Tested directly via resolved_profile_for(name, sid=...).

No mocks: a real AgentRegistry + real on-disk topology/profile/per-session YAML.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.effective import ContextualPermission


def _registry(tmp_path: Path, *, default: str = "inherit") -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda profile: None,
        delegation_capability_default=default,
    )


def _write_per_session(reg: AgentRegistry, name: str, sid: str, body: str) -> None:
    d = reg._session_state_dir(name, sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(body, encoding="utf-8")


def _bind_topology(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / "t.yaml").write_text(
        f"name: t\nkind: network\nmembers: [{member}, peer]\nprofiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


# ── the per-session narrowing applies ───────────────────────────────────────


def test_per_session_config_narrows(tmp_path: Path) -> None:
    """Tier 2: a per-session config.yaml narrows the session's capability (a denied
    tool is denied in the resolved ContextualPermission)."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [sandboxed_exec]\n")
    contextual, _ = reg.resolved_profile_for("worker", sid="task1")
    assert isinstance(contextual, ContextualPermission)
    assert "sandboxed_exec" in contextual.tool_deny


def test_absent_per_session_is_inert(tmp_path: Path) -> None:
    """Tier 2: sid given but NO config.yaml → (None, ∅), unchanged from no narrowing
    (inert-until-spawn)."""
    reg = _registry(tmp_path)
    assert reg.resolved_profile_for("worker", sid="task1") == (None, frozenset())


def test_sid_none_skips_per_session_layer(tmp_path: Path) -> None:
    """Tier 2: sid=None (the default — every current caller) → per-session layer is
    never consulted, even if a config.yaml happens to exist."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [sandboxed_exec]\n")
    assert reg.resolved_profile_for("worker") == (None, frozenset())  # sid omitted


# ── composes ∩ with the topology binding ────────────────────────────────────


def test_composes_with_topology_binding(tmp_path: Path) -> None:
    """Tier 2: the per-session narrowing composes (∪-deny) with the topology-bound
    profile — both denials apply (one combined ContextualLayer)."""
    _bind_topology(tmp_path, member="worker", profile="role",
                   body="name: role\ntool_deny: [delete_file]\n")
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [sandboxed_exec]\n")
    contextual, _ = reg.resolved_profile_for("worker", sid="task1")
    assert {"delete_file", "sandboxed_exec"} <= contextual.tool_deny  # both layers


# ── restrict-only: the per-session layer can NEVER re-grant ─────────────────


def test_per_session_cannot_regrant_topology_deny(tmp_path: Path) -> None:
    """Tier 2: restrict-only (structural) — a per-session config that allow-lists a
    tool the TOPOLOGY denies does NOT re-grant it (∪-deny wins; the per-session layer
    is one more conjunct, can only narrow)."""
    _bind_topology(tmp_path, member="worker", profile="role",
                   body="name: role\ntool_deny: [delete_file]\n")
    reg = _registry(tmp_path)
    # the per-session config tries to ALLOW delete_file (+ another) — must not re-grant
    _write_per_session(reg, "worker", "task1", "name: s\ntool_allow: [delete_file, read_file]\n")
    contextual, _ = reg.resolved_profile_for("worker", sid="task1")
    assert "delete_file" in contextual.tool_deny  # topology deny survives the allow-list


# ── composes with the #2081 _delegate floor (delegate + per-session) ─────────


def test_composes_with_delegate_floor(tmp_path: Path) -> None:
    """Tier 2: an unbound delegate (deny) with a per-session config gets BOTH the
    _delegate floor AND the per-session narrowing (the floor is a conjunct now, so the
    per-session layer composes WITH it, not instead of it)."""
    reg = _registry(tmp_path, default="deny")
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [read_file]\n")
    contextual, _ = reg.resolved_profile_for("worker", sid="task1", is_delegate=True)
    assert "delegate_to_agent" in contextual.tool_deny  # the _delegate floor
    assert "read_file" in contextual.tool_deny           # the per-session narrowing


def test_2081_floor_unchanged_when_sid_none(tmp_path: Path) -> None:
    """Tier 2: #2081 regression — sid=None (every current caller) leaves the unbound-
    delegate floor unchanged (the restructure is behavior-preserving)."""
    from reyn.security.permissions.capability_profile import _BUILTIN_UNTRUSTED_DENY
    reg = _registry(tmp_path, default="deny")
    contextual, _ = reg.resolved_profile_for("solo", is_delegate=True)  # sid omitted
    assert _BUILTIN_UNTRUSTED_DENY <= contextual.tool_deny


def test_malformed_per_session_skipped(tmp_path: Path) -> None:
    """Tier 2: a per-session config.yaml with invalid YAML syntax is skipped (surfaced
    on stderr, not a crash); with no other layer → (None, ∅). Restrict-only safety:
    skipping only widens toward the agent envelope, never past it."""
    reg = _registry(tmp_path)
    _write_per_session(reg, "worker", "task1", "name: s\ntool_deny: [unclosed\n")  # invalid YAML
    assert reg.resolved_profile_for("worker", sid="task1") == (None, frozenset())
