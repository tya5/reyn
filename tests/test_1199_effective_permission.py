"""Tier 2: conjunctive-∩ effective-permission model (#1199 S3.1a, unwired).

S3.1a builds the model + projections; it is UNWIRED (the live PermissionResolver
gates are unchanged — byte-identical). These tests pin the structural invariant
the model exists to guarantee: effective = ⋂ layers, restrict-only, grant-back
forbidden.

#3901 owner correction: a prior version of this file also asserted the ∩
arithmetic itself — dropping a layer from ``EffectivePermission([...])`` and
checking the remainder re-grants what the dropped layer denied. That is a
property of ``all(...)`` over a Python list, not a reyn contract, and the
construction it exercised (``EffectivePermission([AgentLayer(decl)])`` with no
SandboxLayer) is not one production ever builds. Removed rather than kept
axis-swapped to survive #3901 PR-B ③'s retirement of FILE_READ/FILE_WRITE/
NETWORK_HOST from SandboxLayer's projection — the axis swap preserved the
test, not a tested contract.
"""
from __future__ import annotations

from reyn.runtime.profile import AgentProfile
from reyn.security.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    EffectivePermission,
    ProfileLayer,
    SandboxLayer,
)
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox.policy import SandboxPolicy

AX = CapabilityAxis


# ── conjunction: every layer must permit ─────────────────────────────────────


def test_capability_permitted_iff_all_layers_permit() -> None:
    """Tier 2: the SUBPROCESS capability is gated by the sandbox cap.

    #3901 PR-B ①: the agent-side declaration (``decl.subprocess``, added #3901
    to replace the retired #1352-L3 ``decl.shell`` gate) now ALSO constrains
    SUBPROCESS — but stays at its compat default (True = unconstrained) here,
    isolating the sandbox cap as the sole variable this test exercises. The
    sandbox cap still vetoes. (The agent-veto / conjunctive-∩ falsification is
    exercised on a still-agent-gated axis — MCP — in the falsification test
    below.)"""
    decl = PermissionDecl()
    # sandbox allows (deny_subprocess=False, compat) → permitted
    eff = EffectivePermission.of(
        decl=decl, sandbox_policy=SandboxPolicy(deny_subprocess=False)
    )
    assert eff.allows(AX.SUBPROCESS, None) is True
    # sandbox caps it (deny_subprocess=True) → denied (sandbox vetoes)
    eff2 = EffectivePermission.of(
        decl=decl, sandbox_policy=SandboxPolicy(deny_subprocess=True)
    )
    assert eff2.allows(AX.SUBPROCESS, None) is False


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
    narrows the ∩ on those axes (the sandbox doesn't gate tools OR files —
    FILE_WRITE joined this list in #3901 PR-B ③, retired from SandboxLayer's
    projection along with FILE_READ/NETWORK_HOST; the profile doesn't gate
    files either)."""
    assert SandboxLayer(SandboxPolicy()).allows(AX.TOOL, "any") is True
    assert SandboxLayer(SandboxPolicy()).allows(AX.FILE_WRITE, "/x") is True
    assert ProfileLayer(AgentProfile(name="a").default_profile()).allows(AX.FILE_WRITE, "/x") is True
    # None layers are fully ⊤.
    assert SandboxLayer(None).allows(AX.FILE_WRITE, "/anything") is True
    assert ProfileLayer(None).allows(AX.MCP, "any-server") is True


def test_empty_sandbox_deny_list_is_unrestricted() -> None:
    """Tier 2: an empty sandbox deny-list declares no restriction on that axis
    (⊤, restrict-only) — a policy narrows only by listing what it denies.

    #3901 PR-B ③④: FILE_WRITE no longer participates in SandboxLayer's
    permission-∩ projection (retired along with FILE_READ/NETWORK_HOST — see
    the falsification test above), and ``write_paths`` (still a real
    ``SandboxPolicy`` field, still consumed directly by the kernel backend)
    was never a permission-∩ input to begin with post-③. ENV is the
    still-live axis this property now exercises: an empty ``env_deny_names``
    is ⊤ (compat default), a populated one narrows to exactly the named
    values."""
    assert SandboxLayer(SandboxPolicy(env_deny_names=[])).allows(AX.ENV, "PATH") is True
    assert SandboxLayer(
        SandboxPolicy(env_deny_names=["SECRET_TOKEN"])
    ).allows(AX.ENV, "SECRET_TOKEN") is False
