"""Tier 2: is_write_allowed cutover + the include_decl divergence flag (#1199 S3.1b-2a).

The Workspace write gate (is_write_allowed) is routed through EffectivePermission
with a DECL-LESS AgentLayer (include_decl=False) — byte-identical, preserving the
pre-existing divergence from the op-runtime decl-full require_file_write
(reconciled later in S3.1c). The broad byte-identical guard is the workspace +
permission suites; these pin the flag mechanism + the cutover directly.
"""
from __future__ import annotations

from pathlib import Path

from reyn.permissions.effective import AgentLayer, CapabilityAxis, EffectivePermission
from reyn.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver

AX = CapabilityAxis


def test_include_decl_flag_controls_decl_disjunct() -> None:
    """Tier 2: include_decl gates the file decl-grant disjunct — the mechanism
    that preserves the Workspace(decl-less) vs op-runtime(decl-full) divergence
    byte-identically. Same decl, opposite decisions for an out-of-zone declared
    path."""
    out = "/tmp/s31b2a-declared.txt"  # outside the default write zone
    decl = PermissionDecl(file_write=[{"path": out, "scope": "just_path"}])
    # op-runtime (decl-full): the declared path is honored
    assert AgentLayer(decl, include_decl=True).allows(AX.FILE_WRITE, out) is True
    # Workspace (decl-less): the declared path is NOT honored (preserved divergence)
    assert AgentLayer(decl, include_decl=False).allows(AX.FILE_WRITE, out) is False


def test_is_write_allowed_reproduces_current_logic(tmp_path: Path) -> None:
    """Tier 2: the cutover reproduces is_write_allowed exactly — zone OR
    config-approved OR path-approved; otherwise denied. (decl-less: the gate
    takes no decl, so a declared-but-unapproved path is denied.)"""
    # default write zone (.reyn/) → allowed, no config needed
    r = _make_resolver(tmp_path)
    assert r.is_write_allowed(".reyn/x.txt") is True
    # outside the zone, no approval → denied
    assert r.is_write_allowed("/tmp/s31b2a-out.txt") is False
    # config grants file.write → allowed even outside the zone
    r2 = _make_resolver(tmp_path, config={"file.write": "allow"})
    assert r2.is_write_allowed("/tmp/s31b2a-out.txt") is True
