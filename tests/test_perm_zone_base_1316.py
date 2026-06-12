"""Tier 2: default permission zone is anchored to the resolver's project_root (#1316).

Latent divergence (surfaced in S3.3 mount design): the module-level zone fns
(`_in_default_write_zone` / `_in_default_read_zone`) hardcoded `Path.cwd()` as the
base, while approvals + path-approval use `self._project_root`. When project_root
≠ cwd (project-root discovery, mount-mode, or an eval harness that anchors the
workspace at a repo like /testbed), the zone evaluated against cwd while approvals
evaluated against project_root → a write under the project's OWN `.reyn/` default
zone could be denied. #1316 threads project_root into the zone fns so both bases
match.

No mocks: a real PermissionResolver with project_root ≠ cwd.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver


def _resolver(project_root: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False
    )


@pytest.mark.asyncio
async def test_write_zone_anchored_to_project_root_not_cwd(tmp_path: Path) -> None:
    """Tier 2: #1316 reproduce-first — a write under project_root/.reyn (the
    project's default write zone) is granted when project_root ≠ cwd. FAILS pre-fix
    (zone used cwd → the path was not in the cwd zone → PermissionError, diverging
    from where approvals are anchored)."""
    base = (tmp_path / "proj").resolve()
    (base / ".reyn").mkdir(parents=True)
    r = _resolver(base)
    # under project_root/.reyn = default write zone → granted (no raise)
    await r.require_file_write(PermissionDecl(), str(base / ".reyn" / "scratch.txt"), "t")


@pytest.mark.asyncio
async def test_read_zone_anchored_to_project_root_not_cwd(tmp_path: Path) -> None:
    """Tier 2: #1316 — a read under project_root (the default read zone) is granted
    when project_root ≠ cwd. FAILS pre-fix (zone used cwd)."""
    base = (tmp_path / "proj").resolve()
    base.mkdir(parents=True)
    r = _resolver(base)
    await r.require_file_read(PermissionDecl(), str(base / "src" / "module.py"), "t")


@pytest.mark.asyncio
async def test_protected_path_carveout_also_anchored_to_project_root(tmp_path: Path) -> None:
    """Tier 2: #1316 — the canonical protected-write carve-out (approvals.yaml) is
    evaluated against project_root too, so project_root/.reyn/approvals.yaml is
    NOT default-zone-granted (must go through the gated approval flow) even when
    project_root ≠ cwd."""
    base = (tmp_path / "proj").resolve()
    (base / ".reyn").mkdir(parents=True)
    r = _resolver(base)
    # the approval store under project_root is carved out → not default-granted
    with pytest.raises(PermissionError):
        await r.require_file_write(
            PermissionDecl(), str(base / ".reyn" / "approvals.yaml"), "t"
        )
