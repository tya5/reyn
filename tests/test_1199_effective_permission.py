"""Tier 2: conjunctive-∩ effective-permission model (#1199 S3.1a, unwired).

S3.1a builds the model + projections; it is UNWIRED (the live PermissionResolver
gates are unchanged — byte-identical). These tests pin the structural invariant
the model exists to guarantee: effective = ⋂ layers, restrict-only, grant-back
forbidden — including the ★non-negotiable falsification (removing a layer from
the ∩ re-grants a denied capability → over-grant).
"""
from __future__ import annotations

from reyn.chat.profile import AgentProfile
from reyn.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    EffectivePermission,
    ProfileLayer,
    SandboxLayer,
)
from reyn.permissions.permissions import PermissionDecl
from reyn.sandbox.policy import SandboxPolicy

AX = CapabilityAxis


# ── conjunction: every layer must permit ─────────────────────────────────────


def test_capability_permitted_iff_all_layers_permit() -> None:
    """Tier 2: subprocess is permitted only when BOTH the agent grant AND the
    sandbox cap allow it — a single layer's deny vetoes."""
    decl = PermissionDecl(shell=True)                      # agent grants subprocess
    # agent grants + sandbox allows → permitted
    eff = EffectivePermission.of(
        decl=decl, sandbox_policy=SandboxPolicy(allow_subprocess=True)
    )
    assert eff.allows(AX.SUBPROCESS, None) is True
    # agent grants but sandbox caps it → denied (sandbox vetoes)
    eff2 = EffectivePermission.of(
        decl=decl, sandbox_policy=SandboxPolicy(allow_subprocess=False)
    )
    assert eff2.allows(AX.SUBPROCESS, None) is False
    # sandbox would allow but agent never granted → denied (agent vetoes)
    eff3 = EffectivePermission.of(
        decl=PermissionDecl(shell=False),
        sandbox_policy=SandboxPolicy(allow_subprocess=True),
    )
    assert eff3.allows(AX.SUBPROCESS, None) is False


# ── ★the non-negotiable falsification ─────────────────────────────────────────


def test_falsification_removing_a_layer_regrants_a_denied_capability() -> None:
    """Tier 2: (★required) a layer's deny CANNOT be re-granted downstream — and
    removing that layer from the ∩ makes the over-grant possible, proving the
    deny is load-bearing (restrict-only is a structural property of ⋂).

    network: agent grants the host, sandbox denies network → effective denies.
    Drop the sandbox layer → the host is re-granted (over-grant) → FAIL-shape."""
    decl = PermissionDecl(http_get=[{"host": "api.example.com"}])  # agent grants host
    sandbox = SandboxPolicy(network=False)                          # sandbox denies network

    full = EffectivePermission.of(decl=decl, sandbox_policy=sandbox)
    assert full.allows(AX.NETWORK_HOST, "api.example.com") is False  # ∩ denies

    # FALSIFICATION: drop the denying layer from the ∩ → the deny is re-granted.
    without_sandbox = EffectivePermission([AgentLayer(decl)])
    assert without_sandbox.allows(AX.NETWORK_HOST, "api.example.com") is True  # over-grant

    # Same shape for a profile deny (skill allowlist):
    prof = AgentProfile(name="a", allowed_skills=["allowed_skill"])
    eff = EffectivePermission([AgentLayer(PermissionDecl()), ProfileLayer(prof)])
    assert eff.allows(AX.SKILL, "blocked_skill") is False
    assert EffectivePermission([AgentLayer(PermissionDecl())]).allows(
        AX.SKILL, "blocked_skill"
    ) is True  # remove profile layer → re-granted


# ── zone is the agent-layer baseline (∪), not a separate ∩ restrictor ─────────


def test_file_axes_are_decl_less_zone_or_approved() -> None:
    """Tier 2: #1199 S3.1c-1 — the FILE axes are decl-less (zone OR approved). The
    default zone is the agent baseline; a file decl grant is NOT auto-honored (the
    prior decl-grant disjunct is gone). An out-of-zone path needs an approval."""
    # .reyn/ is the default write zone → allowed with no decl grant.
    assert AgentLayer(PermissionDecl()).allows(AX.FILE_WRITE, ".reyn/x.txt") is True
    # an absolute path outside the zone → denied even WITH a decl grant (decl-less).
    outside = "/tmp/reyn-s31c1-test/out.txt"
    assert AgentLayer(PermissionDecl()).allows(AX.FILE_WRITE, outside) is False
    declared = AgentLayer(
        PermissionDecl(file_write=[{"path": outside, "scope": "just_path"}])
    )
    assert declared.allows(AX.FILE_WRITE, outside) is False  # decl no longer auto-grants
    # an approval (folded into the layer) DOES grant it.
    approved = AgentLayer(
        PermissionDecl(),
        approval_check=lambda axis, value: str(value) == outside,
    )
    assert approved.allows(AX.FILE_WRITE, outside) is True


# ── unconstrained axis = ⊤ (a layer never narrows axes it doesn't own) ────────


def test_unconstrained_axis_is_top() -> None:
    """Tier 2: a layer returns True for axes it doesn't constrain, so it never
    narrows the ∩ on those axes (the sandbox doesn't gate skills; the profile
    doesn't gate files)."""
    assert SandboxLayer(SandboxPolicy()).allows(AX.SKILL, "any") is True
    assert ProfileLayer(AgentProfile(name="a")).allows(AX.FILE_WRITE, "/x") is True
    # None layers are fully ⊤.
    assert SandboxLayer(None).allows(AX.FILE_WRITE, "/anything") is True
    assert ProfileLayer(None).allows(AX.MCP, "any-server") is True


def test_empty_sandbox_path_list_is_unrestricted() -> None:
    """Tier 2: an empty sandbox path list declares no restriction on that axis
    (⊤, restrict-only) — a policy narrows only by listing paths."""
    assert SandboxLayer(SandboxPolicy(write_paths=[])).allows(AX.FILE_WRITE, "/x") is True
    assert SandboxLayer(
        SandboxPolicy(write_paths=["/sandboxed"])
    ).allows(AX.FILE_WRITE, "/elsewhere") is False
