"""Tier 2: skill_improver permissions block declares stdlib skill path read access (B8-NEW-1).

Guards the fix for B8-NEW-1: copy_to_work preprocessor tries to read target skill
DSL files that resolve to an absolute path under src/reyn/stdlib/skills/ — which
may be outside the project root when reyn is invoked from a worktree.

The fix adds file.read entries to skill_improver (and eval_builder) so that
startup_guard can prompt once and save per-skill approval for those paths.

These are Tier 2 OS-invariant tests.  No mocks; real PermissionDecl and
PermissionResolver instances.  tmp_path is used as project root so that the
actual stdlib path is outside the default read zone (deterministic gate check).
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


def test_is_read_allowed_for_skill_improver_with_session_approval(tmp_path, monkeypatch):
    """Tier 2: is_read_allowed returns True for a stdlib skill path when session-approved.

    Uses tmp_path as CWD (via monkeypatch.chdir) so the stdlib absolute path is
    outside the default read zone, making the permission gate exercise the actual
    approval mechanism rather than the default-zone short-circuit.

    Simulates what happens after startup_guard prompts and the user approves:
    session_approve_path records the approval, then is_read_allowed returns True.
    """
    monkeypatch.chdir(tmp_path)

    skill_name = "skill_improver"
    # A concrete stdlib skill path that copy_to_work would access
    target_path = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")

    perm = _resolver(tmp_path)

    # Without approval the path is outside the default zone (CWD = tmp_path) → denied
    assert not perm.is_read_allowed(target_path, skill_name=skill_name), (
        "Pre-condition: stdlib path must not be in default read zone for tmp_path CWD"
    )

    # Simulate startup_guard + user approval (recursive approval of stdlib skills dir)
    perm.session_approve_path(
        str(stdlib_root() / "skills"),
        skill_name,
        "file.read",
        recursive=True,
    )

    assert perm.is_read_allowed(target_path, skill_name=skill_name), (
        "is_read_allowed must return True after session_approve_path for the parent dir"
    )


# ── test (c): approval is skill-scoped ────────────────────────────────────────


def test_is_read_allowed_skill_scoped_other_skill_denied(tmp_path, monkeypatch):
    """Tier 2: stdlib path approval is skill-scoped; other skills remain denied.

    Uses tmp_path as CWD so the stdlib absolute path is outside the default zone.
    Approval for skill_improver must NOT extend to other skills.  This pins the
    skill-level isolation of the permission system: one skill cannot read paths
    approved for a different skill.
    """
    monkeypatch.chdir(tmp_path)

    skill_name = "skill_improver"
    other_skill = "eval_builder"
    target_path = str(stdlib_root() / "skills" / "direct_llm" / "skill.md")

    perm = _resolver(tmp_path)
    # Approve only for skill_improver
    perm.session_approve_path(
        str(stdlib_root() / "skills"),
        skill_name,
        "file.read",
        recursive=True,
    )

    # skill_improver: allowed
    assert perm.is_read_allowed(target_path, skill_name=skill_name)
    # eval_builder (no approval): denied
    assert not perm.is_read_allowed(target_path, skill_name=other_skill), (
        "Approval for skill_improver must not grant access under other_skill"
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
