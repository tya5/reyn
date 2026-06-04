"""Tier 2: S3.1b model extensions + require_mcp cutover (#1199, byte-identical A).

S3.1b-1 extends the S3.1a model to faithfully reproduce the gates (the AgentLayer
MCP fix + the ② approval-fold + interactive-mode) and cuts over require_mcp (the
migration anchor) to read EffectivePermission — the existing require_mcp suite
(tests/test_permissions.py) is the byte-identical decision guard. These tests pin
the new model behavior, especially the ★② grant-back safety.
"""
from __future__ import annotations

from reyn.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    EffectivePermission,
    SandboxLayer,
)
from reyn.permissions.permissions import PermissionDecl
from reyn.sandbox.policy import SandboxPolicy

AX = CapabilityAxis


def test_agent_layer_mcp_intersects_decl_mcp_and_allowed_mcp() -> None:
    """Tier 2: AgentLayer.MCP = decl.mcp ∩ decl.allowed_mcp (the S3.1b fix,
    faithful to require_mcp 1248+1253)."""
    # in grant + allowlist → permitted
    assert AgentLayer(PermissionDecl(mcp=["fs"], allowed_mcp=["fs"])).allows(AX.MCP, "fs")
    # in grant but allowlist excludes → denied (the decl.allowed_mcp conjunct)
    assert not AgentLayer(
        PermissionDecl(mcp=["fs", "web"], allowed_mcp=["fs"])
    ).allows(AX.MCP, "web")
    # allowlist None → no per-skill filter, decl.mcp only
    assert AgentLayer(PermissionDecl(mcp=["fs"], allowed_mcp=None)).allows(AX.MCP, "fs")
    # not in decl.mcp → denied even if allowlist includes it
    assert not AgentLayer(PermissionDecl(mcp=[], allowed_mcp=["fs"])).allows(AX.MCP, "fs")
    # allowlist=[] blocks all
    assert not AgentLayer(PermissionDecl(mcp=["fs"], allowed_mcp=[])).allows(AX.MCP, "fs")


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
