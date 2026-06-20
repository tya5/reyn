"""Shared permission-test helper: build a non-interactive PermissionResolver."""
from __future__ import annotations

from pathlib import Path

from reyn.security.permissions.permissions import PermissionResolver


def make_resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    """Build a non-interactive PermissionResolver backed by tmp_path."""
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=False,
    )
