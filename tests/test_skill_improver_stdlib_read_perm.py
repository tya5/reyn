"""Tier 2: skill_improver permissions block declares stdlib skill path read access (B8-NEW-1).

Guards the fix for B8-NEW-1: copy_to_work preprocessor tries to read target skill
DSL files that resolve to an absolute path under src/reyn/stdlib/skills/ — which
may be outside the project root when reyn is invoked from a worktree.

The fix adds file.read entries to skill_improver (and eval_builder) so that
startup_guard can prompt once and save per-skill approval for those paths.

B11-NEW-1 fix update: stdlib_root() is now always in the default read zone
(_in_default_read_zone returns True for paths under the installed reyn stdlib).
Tests that previously used stdlib paths as "out-of-zone" examples now use
tmp directories created via tmp_path_factory (genuinely outside both CWD and stdlib).

These are Tier 2 OS-invariant tests.  No mocks; real PermissionDecl and
PermissionResolver instances.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.skill.skill_paths import stdlib_root


# ── helpers ────────────────────────────────────────────────────────────────────


def _load_skill_permissions(skill_name: str) -> PermissionDecl:
    """Load the PermissionDecl declared in a stdlib skill's frontmatter."""
    import yaml
    from reyn.compiler.parser import _split_frontmatter

    skill_md = stdlib_root() / "skills" / skill_name / "skill.md"
    text = skill_md.read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(text)
    return PermissionDecl.from_dict((fm or {}).get("permissions"))


def _resolver(project_root: Path, *, interactive: bool = False) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=project_root,
        interactive=interactive,
    )


# ── test (a): declaration presence ─────────────────────────────────────────────


def test_skill_improver_decl_includes_stdlib_read():
    """Tier 2: skill_improver PermissionDecl declares read access for stdlib skills path.

    Guards B8-NEW-1: copy_to_work reads target skill DSL files that resolve under
    src/reyn/stdlib/skills/ when the target is a stdlib skill.  The declaration
    must be present so startup_guard can surface a user approval prompt.
    """
    decl = _load_skill_permissions("skill_improver")
    paths = [entry["path"] for entry in decl.file_read]
    assert any("stdlib" in p and "skills" in p for p in paths), (
        f"skill_improver.permissions.file.read must include a stdlib skills path; "
        f"got: {paths}"
    )


def test_skill_improver_decl_includes_local_and_project_read():
    """Tier 2: skill_improver PermissionDecl declares read access for local/project skill paths.

    copy_to_work must also be able to read skills stored in reyn/local and
    reyn/project (user-authored skills).  Both paths must be declared.
    """
    decl = _load_skill_permissions("skill_improver")
    paths = [entry["path"] for entry in decl.file_read]
    assert any("local" in p for p in paths), (
        f"skill_improver.permissions.file.read must include reyn/local; got: {paths}"
    )
    assert any("project" in p for p in paths), (
        f"skill_improver.permissions.file.read must include reyn/project; got: {paths}"
    )


def test_skill_improver_decl_stdlib_path_is_recursive():
    """Tier 2: skill_improver stdlib file.read declaration uses recursive scope.

    The copy_to_work preprocessor reads skill.md and phases/*.md — multiple
    files under the skill directory.  The scope must be 'recursive' so that
    a single startup_guard approval covers the entire skill directory tree,
    not just one file.
    """
    decl = _load_skill_permissions("skill_improver")
    stdlib_entries = [
        entry for entry in decl.file_read
        if "stdlib" in entry.get("path", "") and "skills" in entry.get("path", "")
    ]
    assert stdlib_entries, "No stdlib skills entry found in skill_improver file.read"
    for entry in stdlib_entries:
        assert entry.get("scope") == "recursive", (
            f"stdlib skills entry scope must be 'recursive'; got {entry!r}"
        )


# ── test (b): is_read_allowed with session approval ───────────────────────────


def test_is_read_allowed_for_skill_improver_stdlib_in_default_zone(tmp_path, monkeypatch):
    """Tier 2: stdlib paths are readable by default — no explicit approval needed (B11-NEW-1 fix).

    B11-NEW-1 fix: _in_default_read_zone now includes the installed reyn stdlib as a
    second default zone (in addition to CWD).  This means stdlib paths are always
    readable without explicit session_approve_path, even when CWD is a worktree that
    doesn't contain the stdlib.

    This test pins the B11-NEW-1 invariant: with CWD = tmp_path (not under stdlib),
    a path inside stdlib_root() is still readable without any approval.
    """
    monkeypatch.chdir(tmp_path)

    skill_name = "skill_improver"
    # A concrete stdlib skill path that copy_to_work would access
    target_path = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")

    perm = _resolver(tmp_path)

    # B11-NEW-1 fix: stdlib is always in the default zone — readable without approval
    assert perm.is_read_allowed(target_path, skill_name=skill_name), (
        "B11-NEW-1 fix: stdlib path must be in default read zone even when CWD differs"
    )


# ── test (c): approval is skill-scoped ────────────────────────────────────────


def test_is_read_allowed_skill_scoped_other_skill_denied(tmp_path, tmp_path_factory, monkeypatch):
    """Tier 2: session_approve_path is skill-scoped; other skills remain denied.

    Pins the skill-level isolation of the permission system: approval of an out-of-zone
    path for one skill must NOT extend to a different skill.

    NOTE: stdlib paths (under stdlib_root()) are now in the default read zone for ALL
    skills (B11-NEW-1 fix), so they cannot serve as an example of "out-of-zone" paths.
    This test uses a sibling tmp directory created with tmp_path_factory — a path that
    is genuinely outside both CWD and the stdlib, so skill isolation is exercised on
    real out-of-zone data.
    """
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    external = tmp_path_factory.mktemp("external_skill_scoped")
    monkeypatch.chdir(cwd_dir)

    skill_name = "skill_improver"
    other_skill = "eval_builder"
    target_path = str(external / "some_skill" / "skill.md")

    perm = _resolver(cwd_dir)
    # Approve only for skill_improver
    perm.session_approve_path(
        str(external),
        skill_name,
        "file.read",
        recursive=True,
    )

    # skill_improver: allowed (approved)
    assert perm.is_read_allowed(target_path, skill_name=skill_name)
    # eval_builder (no approval): denied
    assert not perm.is_read_allowed(target_path, skill_name=other_skill), (
        "Approval for skill_improver must not grant access to eval_builder"
    )
    # No skill_name: denied
    assert not perm.is_read_allowed(target_path, skill_name=""), (
        "Approval for skill_improver must not grant anonymous access"
    )


# ── eval_builder also declares the same pattern ───────────────────────────────


def test_eval_builder_decl_includes_stdlib_read():
    """Tier 2: eval_builder PermissionDecl declares read access for stdlib skills path.

    analyze_skill reads target skill DSL files via file ops in act turns.
    The same B8-NEW-1 pattern applies: the declaration must be present so
    startup_guard surfaces the prompt when the stdlib skill is the target.
    """
    decl = _load_skill_permissions("eval_builder")
    paths = [entry["path"] for entry in decl.file_read]
    assert any("stdlib" in p and "skills" in p for p in paths), (
        f"eval_builder.permissions.file.read must include a stdlib skills path; "
        f"got: {paths}"
    )
