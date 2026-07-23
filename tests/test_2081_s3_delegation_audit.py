"""Tier 2: #2081 S3 — the gateway:delegation-unsafe audit (reachability-precise).

`reyn audit` flags, per dangerous CLASS, a delegate-REACHABLE bound capability_profile
(or the _delegate.yaml override) that re-grants the class — re-delegation/exec=HIGH,
memory-write/destructive-FS=MED — plus an INFO posture nudge when capability_default=
inherit while a topology permits delegation.

The deciding property (OPT-A over OPT-B): reachability-precision. HIGH gates exit(1),
so flagging a NON-delegate-target (e.g. a pipeline head that legitimately holds
delegate_to_agent, outbound-only) would be a FALSE deploy-block. A delegation target =
a member with an inbound can_send edge (the A2A request path is can_send-gated).

No mocks: real Topology/CapabilityProfile YAML on disk + real load_config (tmp reyn.yaml)
+ the real audit rule function.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.cli.commands import audit

_RULE = "gateway:delegation-unsafe"


def _profiles(tmp_path: Path) -> Path:
    d = tmp_path / ".reyn" / "capability_profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _topos(tmp_path: Path) -> Path:
    d = tmp_path / ".reyn" / "topologies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reyn_yaml(tmp_path: Path, capability_default: str) -> None:
    (tmp_path / "reyn.yaml").write_text(
        f"delegation:\n  capability_default: {capability_default}\n", encoding="utf-8",
    )


def _findings(monkeypatch, tmp_path: Path) -> list:
    monkeypatch.chdir(tmp_path)
    return audit._gateway_delegation()


def _by(findings: list, *, severity: str | None = None, contains: str | None = None) -> list:
    out = [f for f in findings if f.rule == _RULE]
    if severity is not None:
        out = [f for f in out if f.severity == severity]
    if contains is not None:
        out = [f for f in out if contains in f.detail or contains in f.location]
    return out


# ── OPT-A: a delegate-REACHABLE re-grant is flagged (per-class severity) ─────


def test_reachable_role_regrant_flagged_high(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a delegate-reachable role (network member, inbound edge) bound to a
    permissive profile that permits re-delegation → a HIGH finding."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [worker, peer]\nprofiles:\n  worker: loose\n",
        encoding="utf-8",
    )
    (_profiles(tmp_path) / "loose.yaml").write_text(
        "name: loose\ntool_deny: []\n", encoding="utf-8",  # permits everything
    )
    high = _by(_findings(monkeypatch, tmp_path), severity="HIGH", contains="re-delegation")
    assert high and "worker" in high[0].location


def test_memory_write_regrant_is_med(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: per-class severity — a re-granted memory-write is MED (not HIGH)."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [worker, peer]\nprofiles:\n  worker: loose\n",
        encoding="utf-8",
    )
    (_profiles(tmp_path) / "loose.yaml").write_text("name: loose\ntool_deny: []\n", encoding="utf-8")
    med = _by(_findings(monkeypatch, tmp_path), severity="MED", contains="memory-write")
    assert med


def test_mcp_install_regrant_flagged_high(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: mcp-install is a floored class (single-sourced) → a re-granted
    mcp-install is flagged HIGH. A `tool_deny: []` binding REPLACES the floor's deny,
    so the delegate CAN install servers — capability escalation, peer of re-deleg/exec."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [worker, peer]\nprofiles:\n  worker: loose\n",
        encoding="utf-8",
    )
    (_profiles(tmp_path) / "loose.yaml").write_text("name: loose\ntool_deny: []\n", encoding="utf-8")
    assert _by(_findings(monkeypatch, tmp_path), severity="HIGH", contains="mcp-install")


# ── OPT-A correctness: an outbound-only role is NOT a target → NOT flagged ───


def test_outbound_only_head_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the deciding property — a pipeline HEAD (outbound-only, no inbound
    can_send) bound to a profile permitting re-delegation is NOT a delegation target,
    so it is NOT flagged. (OPT-B would false-flag it HIGH → a wrong exit(1) deploy-block.)"""
    _reyn_yaml(tmp_path, "deny")
    # pipeline a→b→c: `a` has NO inbound edge (not a delegation target).
    (_topos(tmp_path) / "p.yaml").write_text(
        "name: p\nkind: pipeline\nmembers: [a, b, c]\nprofiles:\n  a: loose\n",
        encoding="utf-8",
    )
    (_profiles(tmp_path) / "loose.yaml").write_text("name: loose\ntool_deny: []\n", encoding="utf-8")
    # `a` is bound + permissive, but outbound-only → no per-class finding for it.
    assert not _by(_findings(monkeypatch, tmp_path), contains="/a ")


def test_pipeline_tail_target_is_flagged(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the pipeline TAIL (`c`, inbound from `b`) IS a delegation target — a
    permissive binding there IS flagged (confirms the reachability scope is real, not
    blanket-skip)."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "p.yaml").write_text(
        "name: p\nkind: pipeline\nmembers: [a, b, c]\nprofiles:\n  c: loose\n",
        encoding="utf-8",
    )
    (_profiles(tmp_path) / "loose.yaml").write_text("name: loose\ntool_deny: []\n", encoding="utf-8")
    assert _by(_findings(monkeypatch, tmp_path), severity="HIGH", contains="re-delegation")


# ── the _delegate.yaml override is the global floor → scanned always ─────────


def test_delegate_override_regrant_flagged(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a _delegate.yaml override that re-grants a class is flagged (it IS the
    global delegate floor — every unbound delegate gets it; no reachability needed)."""
    _reyn_yaml(tmp_path, "deny")
    (_profiles(tmp_path) / "_delegate.yaml").write_text(
        "name: _delegate\ntool_deny: []\n", encoding="utf-8",  # a fully-permissive override
    )
    findings = _by(_findings(monkeypatch, tmp_path), contains="_delegate.yaml")
    assert any(f.severity == "HIGH" for f in findings)  # re-delegation/exec
    assert any(f.severity == "MED" for f in findings)   # memory-write/destructive-FS


# ── a DENYING delegate-reachable profile is safe → not flagged ──────────────


def test_reachable_role_that_denies_is_clean(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a delegate-reachable role bound to a profile that DENIES the dangerous
    tools is safe — no finding (the audit flags re-grants, not safe narrowings)."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [worker, peer]\nprofiles:\n  worker: tight\n",
        encoding="utf-8",
    )
    # genuinely tight: deny BOTH the qualified catalog names AND the bare unwrapped
    # aliases (#2111 — the audit taxonomy covers every invocable form, so a profile
    # denying only the qualified form would still re-grant the bare one).
    (_profiles(tmp_path) / "tight.yaml").write_text(
        "name: tight\ntool_deny: [delegate_to_agent, multi_agent__delegate, exec, "
        "exec__run, "
        "delete_file, file__delete, memory_operation__remember_shared, "
        "memory_operation__remember_agent, memory_operation__forget, remember_shared, "
        "remember_agent, forget_memory, mcp__install_registry, mcp__install_package, "
        "mcp__install_local, mcp_install_registry, mcp_install_package, mcp_install_local, "
        "skill_management__install_local, skill_install_local, "  # #2548 PR-C: skill-install class
        "skill_management__install_source, skill_install_source, "  # #2548 PR-D: source install
        "pipeline_management__install_local, pipeline_install_local, "  # pipeline-install class (mirrors skill-install)
        "pipeline_management__install_source, pipeline_install_source, "  # pipeline source install
        "session_spawn, agent_spawn, topology_create, "  # #2103: the full spawn class (session + agent + topology)
        "pipeline__run, run_pipeline, "  # IS-1: pipeline-run class (spawn-adjacent)
        "pipeline__run_async, run_pipeline_async, "  # IS-2: async launch, same class
        "pipeline__run_inline, run_pipeline_inline, "  # IS-4: inline launch, same class
        "pipeline__run_inline_async, run_pipeline_inline_async]\n",  # IS-4: inline async
        encoding="utf-8",
    )
    assert not _by(_findings(monkeypatch, tmp_path), contains="worker")


# ── OPT-C: inherit posture nudge (INFO, never a block) ──────────────────────


def test_inherit_with_delegation_edge_is_info(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: capability_default=inherit + a topology with a delegation edge → an INFO
    posture nudge (delegates inherit full capability). Not HIGH — inherit is the default."""
    _reyn_yaml(tmp_path, "inherit")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [a, b]\n", encoding="utf-8",
    )
    info = _by(_findings(monkeypatch, tmp_path), severity="INFO")
    assert info and "inherit" in info[0].detail


def test_deny_no_bindings_is_clean(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: deny + a topology with edges but no permissive bindings → no findings
    (unbound delegates get the safe floor; nothing to flag)."""
    _reyn_yaml(tmp_path, "deny")
    (_topos(tmp_path) / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [a, b]\n", encoding="utf-8",
    )
    assert _by(_findings(monkeypatch, tmp_path)) == []
