"""Tier 2: reyn skill rollback / versions CLI (FP-0006 Component E).

Tests the `reyn skill versions` and `reyn skill rollback` subcommands using
real filesystem operations (tmp_path).  No mocks — skill resolution uses the
real resolve_skill_path() logic; stdlib refusal uses the real is_stdlib_skill().
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.skill import (
    _VERSIONS_DIR,
    _atomic_write,
    _collect_snapshots,
    _read_current,
    cmd_rollback,
    cmd_versions,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _versions_args(skill_name: str) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    return ns


def _rollback_args(skill_name: str, *, to: str | None = None) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.target_version = to
    return ns


def _make_project_skill(root: Path, skill_name: str, content: str = "current content") -> Path:
    """Create a project skill directory with skill.md."""
    skill_dir = root / "reyn" / "project" / skill_name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "skill.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def _make_versions(root: Path, skill_name: str, versions: dict[int, str], current: int) -> Path:
    """Create .reyn/skill-versions/<skill_name>/ with vN.md files and current pointer."""
    ver_dir = root / ".reyn" / "skill-versions" / skill_name
    ver_dir.mkdir(parents=True)
    for num, content in versions.items():
        (ver_dir / f"v{num}.md").write_text(content, encoding="utf-8")
    (ver_dir / "current").write_text(str(current), encoding="utf-8")
    return ver_dir


# ---------------------------------------------------------------------------
# test_versions_lists_existing_snapshots
# ---------------------------------------------------------------------------


def test_versions_lists_existing_snapshots(tmp_path, monkeypatch, capsys):
    """Tier 2: versions lists v1 and v2 files; marks the current version."""
    monkeypatch.chdir(tmp_path)

    _make_versions(tmp_path, "test_skill", {1: "v1 content", 2: "v2 content"}, current=2)

    cmd_versions(_versions_args("test_skill"))

    out = capsys.readouterr().out
    assert "v1" in out
    assert "v2" in out
    assert "-> current" in out
    # v2 should be marked current, v1 should not
    lines = out.splitlines()
    v1_line = next((l for l in lines if "v1" in l and "v2" not in l), "")
    v2_line = next((l for l in lines if "v2" in l), "")
    assert "-> current" not in v1_line
    assert "-> current" in v2_line


# ---------------------------------------------------------------------------
# test_versions_handles_missing_directory
# ---------------------------------------------------------------------------


def test_versions_handles_missing_directory(tmp_path, monkeypatch, capsys):
    """Tier 2: versions prints graceful message and exits 0 if no snapshots exist."""
    monkeypatch.chdir(tmp_path)

    # No .reyn/skill-versions/some_skill/ directory exists.
    cmd_versions(_versions_args("some_skill"))

    out = capsys.readouterr().out
    assert "No versions saved" in out
    assert "some_skill" in out


# ---------------------------------------------------------------------------
# test_rollback_restores_skill_md_content
# ---------------------------------------------------------------------------


def test_rollback_restores_skill_md_content(tmp_path, monkeypatch, capsys):
    """Tier 2: rollback --to v1 overwrites skill.md with v1 content and updates current."""
    monkeypatch.chdir(tmp_path)

    skill_md = _make_project_skill(tmp_path, "test_skill", "version 3 content")
    _make_versions(
        tmp_path, "test_skill",
        {1: "version 1 content", 2: "version 2 content", 3: "version 3 content"},
        current=3,
    )

    cmd_rollback(_rollback_args("test_skill", to="v1"))

    assert skill_md.read_text(encoding="utf-8") == "version 1 content"

    current_file = tmp_path / ".reyn" / "skill-versions" / "test_skill" / "current"
    assert current_file.read_text(encoding="utf-8").strip() == "1"

    out = capsys.readouterr().out
    assert "v3" in out
    assert "v1" in out
    assert "Rolled back" in out


# ---------------------------------------------------------------------------
# test_rollback_default_goes_to_previous_version
# ---------------------------------------------------------------------------


def test_rollback_default_goes_to_previous_version(tmp_path, monkeypatch, capsys):
    """Tier 2: rollback without --to defaults to current-1 (v2 when current is v3)."""
    monkeypatch.chdir(tmp_path)

    skill_md = _make_project_skill(tmp_path, "test_skill", "version 3 content")
    _make_versions(
        tmp_path, "test_skill",
        {1: "version 1 content", 2: "version 2 content", 3: "version 3 content"},
        current=3,
    )

    cmd_rollback(_rollback_args("test_skill"))

    assert skill_md.read_text(encoding="utf-8") == "version 2 content"

    current_file = tmp_path / ".reyn" / "skill-versions" / "test_skill" / "current"
    assert current_file.read_text(encoding="utf-8").strip() == "2"

    out = capsys.readouterr().out
    assert "v3" in out
    assert "v2" in out


# ---------------------------------------------------------------------------
# test_rollback_refuses_stdlib_skill
# ---------------------------------------------------------------------------


def test_rollback_refuses_stdlib_skill(tmp_path, monkeypatch, capsys):
    """Tier 2: rollback of a stdlib skill is refused with an explanatory message."""
    monkeypatch.chdir(tmp_path)

    # We need a stdlib skill name that actually resolves.  Use 'ops_report'
    # which is present in src/reyn/stdlib/skills/.  We do NOT change CWD to
    # tmp_path in a way that would shadow it — stdlib resolution is absolute.
    # However, since monkeypatch.chdir() changed CWD, resolve_skill_path()
    # will look in:
    #   reyn/local/ops_report (missing)
    #   reyn/project/ops_report (missing)
    #   <stdlib_root>/skills/ops_report (present — stdlib)
    # That should trigger the stdlib guard.

    # Make a fake versions dir so rollback doesn't fail before the stdlib check.
    _make_versions(tmp_path, "ops_report", {1: "v1", 2: "v2"}, current=2)

    with pytest.raises(SystemExit) as exc_info:
        cmd_rollback(_rollback_args("ops_report"))

    assert exc_info.value.code == 1

    err = capsys.readouterr().err
    assert "Cannot roll back stdlib skill" in err
    assert "read-only" in err


# ---------------------------------------------------------------------------
# test_rollback_errors_on_missing_target_version
# ---------------------------------------------------------------------------


def test_rollback_errors_on_missing_target_version(tmp_path, monkeypatch, capsys):
    """Tier 2: rollback --to v99 when v99 doesn't exist exits 2 with clear error."""
    monkeypatch.chdir(tmp_path)

    _make_project_skill(tmp_path, "test_skill", "current content")
    _make_versions(
        tmp_path, "test_skill",
        {1: "v1 content", 2: "v2 content", 3: "v3 content"},
        current=3,
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_rollback(_rollback_args("test_skill", to="v99"))

    assert exc_info.value.code == 2

    err = capsys.readouterr().err
    assert "v99" in err
    assert "not found" in err
