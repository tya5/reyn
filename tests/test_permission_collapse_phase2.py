"""Tier 2: #571 collapse arc Phase 2 — canonical-paths exception + compat shim.

Verifies the OS-invariants introduced by Phase 2:

1. The three canonical protected paths (.reyn/mcp.yaml / .reyn/cron.yaml /
   .reyn/index/sources.yaml) are excepted from the broad `.reyn/`
   default write zone — direct `safe.file.write` to them requires an
   explicit `file.write: [{path: ...}]` declaration.
2. The bool-axis compat shim in `PermissionDecl.from_dict` expands each
   set bool axis (mcp_install / mcp_drop_server / cron_register /
   index_drop) into the equivalent `file.write` entry, so existing
   skills written before the collapse keep working through `require_file_write`.
3. Non-canonical paths under `.reyn/` (= chunkers, cursors, scratch
   state) are unaffected — the broad default zone still covers them.

Tier policy: these are OS-invariant tests pinning the contract between
the permission resolver and `reyn.safe.file`. They use real
PermissionResolver instances + the real safe.file module — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import (
    PermissionDecl,
    PermissionResolver,
    _CANONICAL_PROTECTED_WRITE_PATHS,
    _in_default_write_zone,
    _is_canonical_protected_write,
)


# ── default-zone exception ─────────────────────────────────────────────────────


def test_canonical_protected_paths_excepted_from_default_zone(tmp_path, monkeypatch):
    """Tier 2: the 3 canonical paths return False from _in_default_write_zone."""
    monkeypatch.chdir(tmp_path)
    for rel in _CANONICAL_PROTECTED_WRITE_PATHS:
        assert _in_default_write_zone(rel) is False, (
            f"{rel!r} should be excepted from the default write zone (Gap A)"
        )
        assert _is_canonical_protected_write(rel) is True


def test_other_reyn_paths_still_in_default_zone(tmp_path, monkeypatch):
    """Tier 2: chunker / cursor / scratch paths under .reyn/ still default-allowed."""
    monkeypatch.chdir(tmp_path)
    for rel in (
        ".reyn/index/events_cursor",
        ".reyn/index/chunks.jsonl",
        ".reyn/approvals.yaml",
        ".reyn/events.jsonl",
        ".reyn/scratch/anything.txt",
        "reyn/local/whatever.py",
    ):
        assert _in_default_write_zone(rel) is True, (
            f"{rel!r} should remain in the default write zone (non-canonical)"
        )
        assert _is_canonical_protected_write(rel) is False


def test_require_file_write_rejects_canonical_without_decl(tmp_path, monkeypatch):
    """Tier 2: require_file_write raises for protected path without explicit decl."""
    monkeypatch.chdir(tmp_path)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl()  # no axes set
    with pytest.raises(PermissionError, match="was not approved"):
        resolver.require_file_write(decl, ".reyn/mcp.yaml", "skill_x")


def test_require_file_write_accepts_canonical_after_session_approval(tmp_path, monkeypatch):
    """Tier 2: require_file_write passes when path was approved at startup_guard time.

    Phase 2 does not change require_file_write semantics — declaration alone
    does NOT pass; operator must approve (via startup_guard or persisted
    approval). This test simulates the post-startup-guard state.
    """
    monkeypatch.chdir(tmp_path)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl(file_write=[{"path": ".reyn/mcp.yaml", "scope": "just_path"}])
    resolver.session_approve_path(".reyn/mcp.yaml", "skill_x", "file.write")
    # Should not raise — startup_guard's session approval covers the path.
    resolver.require_file_write(decl, ".reyn/mcp.yaml", "skill_x")


# ── compat shim ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "axis,expected_path",
    [
        ("mcp_install",     ".reyn/mcp.yaml"),
        ("mcp_drop_server", ".reyn/mcp.yaml"),
        ("cron_register",   ".reyn/cron.yaml"),
        ("index_drop",      ".reyn/index/sources.yaml"),
    ],
)
def test_bool_axis_compat_shim_expands_to_file_write(axis, expected_path):
    """Tier 2: setting a bool axis via from_dict auto-adds the canonical file.write entry."""
    decl = PermissionDecl.from_dict({axis: True})
    assert getattr(decl, axis) is True, f"bool axis {axis} should remain set"
    paths = {entry.get("path") for entry in decl.file_write if isinstance(entry, dict)}
    assert expected_path in paths, (
        f"compat shim should have added {expected_path!r} to file_write for {axis}"
    )


def test_compat_shim_does_not_duplicate_explicit_entry():
    """Tier 2: explicit file.write of a canonical path is not duplicated by the shim."""
    decl = PermissionDecl.from_dict({
        "mcp_install": True,
        "file.write": [{"path": ".reyn/mcp.yaml", "scope": "just_path"}],
    })
    matching = [e for e in decl.file_write if e.get("path") == ".reyn/mcp.yaml"]
    assert len(matching) == 1, "explicit + implicit should de-duplicate to a single entry"


def test_compat_shim_no_bool_axis_no_implicit_entries():
    """Tier 2: when no bool axis is set, file_write only contains explicit entries."""
    decl = PermissionDecl.from_dict({
        "file.write": [{"path": "/tmp/x", "scope": "just_path"}],
    })
    paths = {entry.get("path") for entry in decl.file_write if isinstance(entry, dict)}
    assert paths == {"/tmp/x"}
    for canonical in _CANONICAL_PROTECTED_WRITE_PATHS:
        assert canonical not in paths


def test_startup_guard_skips_canonical_when_bool_axis_set(tmp_path, monkeypatch):
    """Tier 2: startup_guard does not prompt for canonical paths when the bool axis is set.

    Avoids double-prompting: operator already approves the operation via
    the bool axis's per-op prompt at op-execution time, so prompting again
    at startup for the equivalent ``file.write`` entry would be redundant.
    """
    monkeypatch.chdir(tmp_path)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    decl = PermissionDecl.from_dict({"mcp_install": True})

    # The compat shim populated decl.file_write with the canonical path.
    assert any(
        e.get("path") == ".reyn/mcp.yaml" for e in decl.file_write
    ), "compat shim should add the canonical file.write entry"

    # Walk the startup_guard prompt-collection logic directly: build the
    # set of paths that would be prompted, and verify the canonical
    # entry is skipped.
    from reyn.permissions.permissions import _in_default_write_zone
    canonical_skip = set()
    for axis, paths in PermissionDecl._BOOL_AXIS_TO_FILE_WRITE.items():
        if getattr(decl, axis, False):
            canonical_skip.update(paths)
    prompt_paths = [
        entry["path"]
        for entry in decl.file_write
        if entry.get("path")
        and entry["path"] not in canonical_skip
        and not _in_default_write_zone(entry["path"])
        and not resolver._is_path_approved_for(entry["path"], "skill_x", "file.write")
    ]
    assert ".reyn/mcp.yaml" not in prompt_paths, (
        "canonical-with-bool-axis path should be skipped from startup prompts"
    )


def test_startup_guard_still_prompts_canonical_without_bool_axis(tmp_path, monkeypatch):
    """Tier 2: startup_guard DOES prompt for canonical paths declared explicitly without bool axis."""
    monkeypatch.chdir(tmp_path)
    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path)
    # Explicit file.write without the bool axis.
    decl = PermissionDecl.from_dict({
        "file.write": [{"path": ".reyn/mcp.yaml", "scope": "just_path"}],
    })
    assert decl.mcp_install is False

    from reyn.permissions.permissions import _in_default_write_zone
    canonical_skip = set()
    for axis, paths in PermissionDecl._BOOL_AXIS_TO_FILE_WRITE.items():
        if getattr(decl, axis, False):
            canonical_skip.update(paths)
    prompt_paths = [
        entry["path"]
        for entry in decl.file_write
        if entry.get("path")
        and entry["path"] not in canonical_skip
        and not _in_default_write_zone(entry["path"])
        and not resolver._is_path_approved_for(entry["path"], "skill_x", "file.write")
    ]
    assert ".reyn/mcp.yaml" in prompt_paths, (
        "explicit-only declaration (no bool axis) should still prompt at startup"
    )


# ── reyn.safe.file enforcement ────────────────────────────────────────────────


def test_safe_file_check_write_rejects_canonical_via_parent_dir(tmp_path, monkeypatch):
    """Tier 2: safe.file._check_write rejects a canonical path covered only by parent dir."""
    monkeypatch.chdir(tmp_path)
    from reyn.safe import file as safe_file

    # Simulate the preprocessor_executor wiring: .reyn/ in write_paths via prefix.
    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path / ".reyn"), str(tmp_path / "reyn")],
    )
    with pytest.raises(PermissionError, match="canonical protected path"):
        safe_file._check_write(str(tmp_path / ".reyn" / "mcp.yaml"))


def test_safe_file_check_write_accepts_canonical_via_explicit_path(tmp_path, monkeypatch):
    """Tier 2: safe.file._check_write accepts canonical path when listed explicitly."""
    monkeypatch.chdir(tmp_path)
    from reyn.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[
            str(tmp_path / ".reyn"),
            str(tmp_path / "reyn"),
            str(tmp_path / ".reyn" / "mcp.yaml"),  # explicit
        ],
    )
    # Should not raise.
    safe_file._check_write(str(tmp_path / ".reyn" / "mcp.yaml"))


def test_safe_file_check_write_still_allows_non_canonical_under_reyn(tmp_path, monkeypatch):
    """Tier 2: non-canonical .reyn/ paths still pass via the broad default zone."""
    monkeypatch.chdir(tmp_path)
    from reyn.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path / ".reyn"), str(tmp_path / "reyn")],
    )
    # Cursor file under .reyn/index/ but NOT sources.yaml → still allowed.
    safe_file._check_write(str(tmp_path / ".reyn" / "index" / "events_cursor"))
    safe_file._check_write(str(tmp_path / ".reyn" / "approvals.yaml"))
