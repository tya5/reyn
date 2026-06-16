"""Tier 2: is_read_allowed + require_file_read/write cutovers (#1199 S3.1b-2b).

These gates route through the unified model. #1199 S3.1c-1 made them all
DECL-LESS (zone OR approved) — the op-runtime require_file_* gates no longer
honor declared paths, matching is_read/write_allowed (the S3.1b-2 transitional
divergence is resolved). The decl-less behavior + divergence resolution are
pinned in test_1199_s31c1_swebench_only; these keep the cutover invariants.
(require_file_* are sync.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver

# #1199 S3.1c-1: the require_file_read/write decl-full auto-grant was removed —
# both gates are now decl-less (zone OR approved). The tests that pinned the old
# decl-full "honors declared path" behavior are deleted here; the new decl-less
# behavior + the divergence resolution are pinned in test_1199_s31c1_swebench_only.


def test_is_read_allowed_reproduces_current_logic(tmp_path: Path) -> None:
    """Tier 2: is_read_allowed reproduces zone OR config OR path (decl-less) — and
    does NOT honor decl (it takes none; the preserved divergence)."""
    r = _make_resolver(tmp_path)
    assert r.is_read_allowed("a-file-under-cwd.txt") is True   # relative → read zone (CWD)
    assert r.is_read_allowed("/tmp/s31b2b-out.txt") is False   # outside, no approval
    r2 = _make_resolver(tmp_path, config={"file.read": "allow"})
    assert r2.is_read_allowed("/tmp/s31b2b-out.txt") is True   # config grant
