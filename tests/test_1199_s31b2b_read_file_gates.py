"""Tier 2: is_read_allowed + require_file_read/write cutovers (#1199 S3.1b-2b).

Continues the S3.1b-2 A-discipline through the unified model: the Workspace read
gate (is_read_allowed) is DECL-LESS (include_decl=False); the op-runtime file
gates (require_file_read/write) are DECL-FULL (include_decl=True, honoring the
skill's declared paths in non-interactive mode). Each gate's broad byte-identical
guard is its existing permission/workspace suite; these pin the divergence
directly. (require_file_* are sync.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver


def test_require_file_write_decl_full_honors_declared_path(tmp_path: Path) -> None:
    """Tier 2: the op-runtime require_file_write is DECL-FULL — a non-interactive
    skill's declared out-of-zone path is honored (the decl-full side of the
    divergence; the Workspace is_write_allowed denies the same path)."""
    r = _make_resolver(tmp_path)  # non-interactive
    out = "/tmp/s31b2b-declared.txt"
    decl = PermissionDecl(file_write=[{"path": out, "scope": "just_path"}])
    r.require_file_write(decl, out)  # declared → honored (no raise)
    with pytest.raises(PermissionError, match="was not approved"):
        r.require_file_write(PermissionDecl(), out)  # not declared → raises


def test_require_file_read_decl_full_honors_declared_path(tmp_path: Path) -> None:
    """Tier 2: require_file_read decl-full, same shape (FILE_READ)."""
    r = _make_resolver(tmp_path)
    out = "/tmp/s31b2b-read-declared.txt"
    decl = PermissionDecl(file_read=[{"path": out, "scope": "just_path"}])
    r.require_file_read(decl, out)
    with pytest.raises(PermissionError, match="was not approved"):
        r.require_file_read(PermissionDecl(), out)


def test_is_read_allowed_reproduces_current_logic(tmp_path: Path) -> None:
    """Tier 2: is_read_allowed reproduces zone OR config OR path (decl-less) — and
    does NOT honor decl (it takes none; the preserved divergence)."""
    r = _make_resolver(tmp_path)
    assert r.is_read_allowed("a-file-under-cwd.txt") is True   # relative → read zone (CWD)
    assert r.is_read_allowed("/tmp/s31b2b-out.txt") is False   # outside, no approval
    r2 = _make_resolver(tmp_path, config={"file.read": "allow"})
    assert r2.is_read_allowed("/tmp/s31b2b-out.txt") is True   # config grant
