"""Tier 2: S3.1b model extensions + require_mcp cutover (#1199, byte-identical A).

S3.1b-1 extends the S3.1a model to faithfully reproduce the gates (the AgentLayer
MCP fix + the ② approval-fold + interactive-mode) and cuts over require_mcp (the
migration anchor) to read EffectivePermission — the existing require_mcp suite
(tests/test_permissions.py) is the byte-identical decision guard. These tests pin
the new model behavior, especially the ★② grant-back safety.
"""
from __future__ import annotations

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


def _mcp_gate(decl):
    """The require_mcp MCP ∩ stack (#2074 S4a): AgentLayer(grant) ∩
    ProfileLayer(per-agent allowlist). Mirrors permissions.require_mcp."""
    return EffectivePermission(
        [AgentLayer(decl), ProfileLayer.from_allowlists(allowed_mcp=decl.allowed_mcp)]
    )


def test_agent_layer_mcp_is_grant_only_after_s4a() -> None:
    """Tier 2: #2074 S4a moved the per-agent allowlist OUT of AgentLayer.MCP — the
    layer is now the GRANT only (decl.mcp); the allowlist (decl.allowed_mcp) is a
    separate ProfileLayer (symmetric with the SKILL axis)."""
    # grant-only: in decl.mcp → True regardless of allowed_mcp (allowlist not here)
    assert AgentLayer(PermissionDecl(mcp=["fs"], allowed_mcp=[])).allows(AX.MCP, "fs")
    # not in decl.mcp → denied (the grant)
    assert not AgentLayer(PermissionDecl(mcp=[], allowed_mcp=["fs"])).allows(AX.MCP, "fs")
    # the allowlist now lives on ProfileLayer
    assert ProfileLayer.from_allowlists(allowed_mcp=["fs"]).allows(AX.MCP, "fs")
    assert not ProfileLayer.from_allowlists(allowed_mcp=["fs"]).allows(AX.MCP, "web")


def test_mcp_gate_full_intersection_preserved() -> None:
    """Tier 2: the FULL require_mcp ∩ (AgentLayer ∩ ProfileLayer) reproduces the
    pre-S4a ``decl.mcp ∩ decl.allowed_mcp`` decision byte-identically (∩ associative
    — the migration changed the decomposition, not the gate outcome)."""
    # in grant + allowlist → permitted
    assert _mcp_gate(PermissionDecl(mcp=["fs"], allowed_mcp=["fs"])).allows(AX.MCP, "fs")
    # in grant but allowlist excludes → denied
    assert not _mcp_gate(PermissionDecl(mcp=["fs", "web"], allowed_mcp=["fs"])).allows(AX.MCP, "web")
    # allowlist None → no per-agent filter, decl.mcp only
    assert _mcp_gate(PermissionDecl(mcp=["fs"], allowed_mcp=None)).allows(AX.MCP, "fs")
    # not in decl.mcp → denied even if allowlist includes it
    assert not _mcp_gate(PermissionDecl(mcp=[], allowed_mcp=["fs"])).allows(AX.MCP, "fs")
    # allowlist=[] blocks all
    assert not _mcp_gate(PermissionDecl(mcp=["fs"], allowed_mcp=[])).allows(AX.MCP, "fs")


def test_approval_folded_inside_agent_layer_is_restricted_by_conjunction() -> None:
    """Tier 2: (★② grant-back safety) an approval folded INTO the agent layer does
    NOT re-grant what a downstream Sandbox/Profile layer denies — because it's
    inside the ∩, not a top-level `approved OR effective` disjunct. This is the
    security property the ② correction protects."""
    out = "/outside/zone/x.txt"
    approve = lambda axis, value: axis is AX.FILE_WRITE and value == out  # noqa: E731
    agent = AgentLayer(PermissionDecl(), approval_check=approve)
    # the agent layer alone honors the approval
    assert agent.allows(AX.FILE_WRITE, out) is True
    # but a sandbox restricting writes to /sandboxed vetoes — the ∩ DENIES.
    sandbox = SandboxLayer(SandboxPolicy(write_paths=["/sandboxed"]))
    assert EffectivePermission([agent, sandbox]).allows(AX.FILE_WRITE, out) is False
    # (a top-level `approved OR effective` would WRONGLY return True here — the
    # grant-back hole the fold-inside design closes.)


# #1199 S3.1c-1: the interactive-gated decl file-grant disjunct was removed — the
# FILE axes are decl-less (zone OR approved) in every mode, so there is no
# interactive-vs-non-interactive difference to gate. This test (which pinned the
# now-removed `not self._interactive AND decl_covers` disjunct) is deleted; the
# decl-less behavior is pinned in test_1199_s31c1_swebench_only.
