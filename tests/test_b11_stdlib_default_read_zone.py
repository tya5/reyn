"""Tier 2: stdlib_root() paths are always in the default read zone (B11-NEW-1 fix).

Root cause (B11-NEW-1): when reyn runs inside a git worktree via `reyn chat`,
CWD = .../sandbox_2/.claude/worktrees/<id>/ while stdlib_root() returns the
installed-package absolute path (e.g. .../sandbox_2/src/reyn/stdlib).  Under
an editable install (pip install -e .), stdlib_root() is always anchored to the
main repo, not the CWD.

_in_default_read_zone only checked CWD, so stdlib paths were denied in the
preprocessor run_op (file.read) gate even though they are OS-internal files.

Fix: _in_default_read_zone now also checks stdlib_root() as a second default
zone.  These tests pin that invariant.  No mocks — real PermissionResolver
and the real _in_default_read_zone behavior (called via is_read_allowed).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionResolver
from reyn.skill.skill_paths import stdlib_root


def _resolver(project_root: Path, *, interactive: bool = False) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=project_root,
        interactive=interactive,
    )


# ── B11-NEW-1 core invariants ─────────────────────────────────────────────────


def test_stdlib_path_in_default_read_zone_with_foreign_cwd(tmp_path, monkeypatch):
    """Tier 2: stdlib paths are readable when CWD is unrelated to the installed package.

    Pins B11-NEW-1 fix: _in_default_read_zone must return True for stdlib paths
    even when CWD is tmp_path (a directory completely unrelated to the stdlib).
    This simulates running reyn from a git worktree where CWD != stdlib location.
    """
    monkeypatch.chdir(tmp_path)

    perm = _resolver(tmp_path)
    target = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")

    assert perm.is_read_allowed(target, skill_name="any_skill"), (
        "B11-NEW-1 fix: stdlib path must be readable when CWD is outside the stdlib root"
    )


def test_stdlib_path_readable_without_explicit_session_approval(tmp_path, monkeypatch):
    """Tier 2: stdlib paths require no session_approve_path call to be readable.

    Before B11-NEW-1 fix, startup_guard had to issue a session_approve_path for
    every stdlib path declared in the skill's permissions.  After the fix, they
    fall into the default read zone and no explicit approval is needed.
    """
    monkeypatch.chdir(tmp_path)

    perm = _resolver(tmp_path)
    # Multiple different paths under stdlib — all must pass without any approval calls
    paths_under_stdlib = [
        str(stdlib_root() / "skills" / "direct_llm" / "skill.md"),
        str(stdlib_root() / "skills" / "skill_improver" / "skill.md"),
        str(stdlib_root() / "skills" / "eval_builder" / "skill.md"),
        str(stdlib_root() / "skills" / "skill_improver" / "phases" / "copy_to_work.md"),
    ]
    for path in paths_under_stdlib:
        assert perm.is_read_allowed(path, skill_name="skill_improver"), (
            f"B11-NEW-1 fix: {path} must be readable without explicit approval"
        )


def test_stdlib_default_zone_is_skill_agnostic(tmp_path, monkeypatch):
    """Tier 2: stdlib default read zone is NOT skill-scoped — all skills can read it.

    The stdlib is the OS's own bundled files.  Unlike session_approve_path
    (which is skill-scoped), the default zone is global.  Any skill name should
    be able to read stdlib paths without approval.
    """
    monkeypatch.chdir(tmp_path)

    perm = _resolver(tmp_path)
    target = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")

    for skill_name in ("skill_improver", "eval_builder", "direct_llm", "some_custom_skill"):
        assert perm.is_read_allowed(target, skill_name=skill_name), (
            f"B11-NEW-1 fix: stdlib path must be readable for any skill_name (got '{skill_name}')"
        )


def test_stdlib_subtree_fully_in_default_zone(tmp_path, monkeypatch):
    """Tier 2: entire stdlib subtree (not just top-level) is in the default read zone.

    The default zone includes all paths that resolve under stdlib_root(), at
    arbitrary depth.  This test verifies deep nested paths are included.
    """
    monkeypatch.chdir(tmp_path)

    perm = _resolver(tmp_path)
    # Deeply nested paths
    deep_paths = [
        str(stdlib_root() / "skills" / "skill_improver" / "phases" / "copy_to_work.md"),
        str(stdlib_root() / "skills" / "eval_builder" / "phases" / "analyze_skill.md"),
    ]
    for path in deep_paths:
        assert perm.is_read_allowed(path, skill_name="test_skill"), (
            f"B11-NEW-1 fix: deep stdlib path must be in default read zone: {path}"
        )


def test_non_stdlib_external_path_still_denied(tmp_path, tmp_path_factory, monkeypatch):
    """Tier 2: paths outside both CWD and stdlib are still denied (no regression).

    The B11-NEW-1 fix must not widen the default read zone beyond CWD and stdlib.
    An external tmp directory (sibling of tmp_path, not under stdlib) must still
    be denied without explicit approval.
    """
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    external = tmp_path_factory.mktemp("external_b11")
    monkeypatch.chdir(cwd_dir)

    perm = _resolver(cwd_dir)
    target = str(external / "somefile.txt")

    assert not perm.is_read_allowed(target, skill_name="skill_improver"), (
        "External non-stdlib paths must still be denied after B11-NEW-1 fix"
    )


def test_stdlib_default_zone_cwd_still_works(tmp_path, monkeypatch):
    """Tier 2: CWD-based default read zone remains unaffected by B11-NEW-1 fix.

    Adding the stdlib zone must not break the existing CWD zone behavior.
    Files under CWD must still be readable without approval.
    _in_default_read_zone uses Path.cwd() at call time, so monkeypatch.chdir
    is required for the CWD check to see tmp_path.
    """
    monkeypatch.chdir(tmp_path)

    perm = _resolver(tmp_path)
    target = str(tmp_path / "some_project_file.py")

    assert perm.is_read_allowed(target, skill_name="any_skill"), (
        "CWD default zone must still function after B11-NEW-1 fix"
    )
