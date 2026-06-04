"""Tier 2: is_write_allowed cutover (#1199 S3.1b-2a).

The Workspace write gate (is_write_allowed) is routed through EffectivePermission
with a decl-less AgentLayer (zone OR approved). #1199 S3.1c-1 made the op-runtime
require_file_write decl-less too, so the two now agree (divergence resolved; the
former include_decl flag is gone). These pin the is_write_allowed cutover; the
divergence resolution is pinned in test_1199_s31c1_swebench_only.
"""
from __future__ import annotations

from pathlib import Path

from reyn.permissions.effective import AgentLayer, CapabilityAxis, EffectivePermission
from reyn.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver

AX = CapabilityAxis


# #1199 S3.1c-1: the include_decl flag (which gated the file decl-grant disjunct
# to preserve the transitional Workspace-vs-op-runtime divergence) was removed —
# files are decl-less everywhere now, so both gates agree. The flag test is
# deleted; the divergence resolution is pinned in test_1199_s31c1_swebench_only.


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
