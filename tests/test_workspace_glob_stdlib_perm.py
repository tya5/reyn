"""Tier 2: glob_files() boundary check delegates to PermissionResolver for outside-project paths.

Guards that Workspace.glob_files() does NOT hard-raise for absolute paths
outside the project root when PermissionResolver.is_read_allowed() grants
access, and DOES raise when no permission is available.

Test isolation: each test uses tmp_path as base_dir (via monkeypatch.chdir)
so default-zone checks are deterministic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionResolver

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_workspace(
    *,
    project_root: Path,
    permission_resolver: PermissionResolver | None = None,
    skill_name: str = "test_skill",
) -> Workspace:
    events = EventLog()
    ws = Workspace(
        events=events,
        permission_resolver=permission_resolver,
        skill_name=skill_name,
    )
    # Override base_dir / state_dir to match the tmp project root
    ws.base_dir = project_root.resolve()
    ws.state_dir = (project_root / ".reyn").resolve()
    ws.state_dir.mkdir(parents=True, exist_ok=True)
    (ws.state_dir / "artifacts").mkdir(exist_ok=True)
    return ws


def _resolver(
    project_root: Path,
    *,
    config: dict | None = None,
    interactive: bool = False,
) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=project_root,
        interactive=interactive,
    )


# ── tests ─────────────────────────────────────────────────────────────────────


def test_outside_project_glob_without_permission_raises(tmp_path):
    """Tier 2: absolute path glob outside project root with no permission raises PermissionError.

    This preserves the pre-existing security boundary: default-deny for paths
    outside the project unless permission is explicitly granted.
    """
    # Create a separate directory that is outside tmp_path (the project root)
    outside_dir = tmp_path.parent / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "some_file.txt").write_text("hello")

    perm = _resolver(tmp_path)  # no config grants, interactive=False
    ws = _make_workspace(project_root=tmp_path, permission_resolver=perm)

    with pytest.raises(PermissionError, match="outside project"):
        ws.glob_files(str(outside_dir / "*.txt"))


def test_outside_project_glob_with_permission_succeeds(tmp_path):
    """Tier 2: absolute path glob outside project root succeeds when PermissionResolver permits it.

    Simulates the stdlib skill scenario: the skill directory lives outside the
    project root but a file.read permission (recursive) has been session-approved,
    so glob_files() should proceed without raising.
    """
    # Create a simulated stdlib skill directory outside the project root
    stdlib_root = tmp_path.parent / "stdlib_skills"
    skill_dir = stdlib_root / "direct_llm"
    phases_dir = skill_dir / "phases"
    phases_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.md").write_text("# skill")
    (phases_dir / "phase_one.md").write_text("# phase")
    (phases_dir / "phase_two.md").write_text("# phase 2")

    skill_name = "skill_improver"
    perm = _resolver(tmp_path)
    # Session-approve the stdlib skill directory recursively
    perm.session_approve_path(str(skill_dir), skill_name, "file.read", recursive=True)

    ws = _make_workspace(project_root=tmp_path, permission_resolver=perm, skill_name=skill_name)

    # Glob for all .md files under the skill dir
    results = ws.glob_files(str(phases_dir / "*.md"))
    # Both phase files should be found
    assert results, "Expected glob to return at least one .md file"
    assert all(r.endswith(".md") for r in results)


def test_inside_project_absolute_glob_bypasses_permission_check(tmp_path):
    """Tier 2: absolute path glob inside base_dir proceeds without consulting PermissionResolver.

    Paths within the project root are in the default read zone and must not
    be gated by the permission system (existing invariant).
    """
    # Create a file inside the project root
    subdir = tmp_path / "src" / "mymodule"
    subdir.mkdir(parents=True)
    (subdir / "file_a.py").write_text("pass")
    (subdir / "file_b.py").write_text("pass")

    # No permission resolver at all — should still work for inside-project paths
    ws = _make_workspace(project_root=tmp_path, permission_resolver=None)

    results = ws.glob_files(str(subdir / "*.py"))
    assert results, "Expected glob to return at least one .py file"
    assert all(r.endswith(".py") for r in results)


def test_state_dir_absolute_glob_bypasses_permission_check(tmp_path):
    """Tier 2: absolute path glob inside state_dir (.reyn/) proceeds without permission check.

    state_dir is the second default-allowed zone; globs rooted there must not
    require a PermissionResolver.
    """
    state_dir = tmp_path / ".reyn"
    artifacts_dir = state_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "result.json").write_text("{}")
    (artifacts_dir / "meta.json").write_text("{}")

    ws = _make_workspace(project_root=tmp_path, permission_resolver=None)
    # state_dir is set in _make_workspace; confirm glob works
    results = ws.glob_files(str(artifacts_dir / "*.json"))
    assert results, "Expected glob to return at least one .json file"
    assert all(r.endswith(".json") for r in results)
