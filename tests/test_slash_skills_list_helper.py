"""Tier 2: /skills slash — _list_skills pure helper behavioural contracts.

`_list_skills(root)` is the pure directory-scanner that backs the /skills
slash command.  The handler builds its output from three calls to this helper
(stdlib / project / local).  These tests pin the helper's contracts so a
file-system or naming change can't silently break /skills output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.slash.skills import _list_skills


def test_list_skills_missing_root_returns_empty(tmp_path: Path) -> None:
    """Tier 2: a non-existent root path yields [] without raising."""
    result = _list_skills(tmp_path / "no_such_dir")
    assert result == []


def test_list_skills_empty_dir_returns_empty(tmp_path: Path) -> None:
    """Tier 2: a root that exists but has no subdirs yields []."""
    root = tmp_path / "empty"
    root.mkdir()
    assert _list_skills(root) == []


def test_list_skills_subdirs_without_skill_md_are_skipped(tmp_path: Path) -> None:
    """Tier 2: subdirs that lack skill.md are not counted as skills."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "not_a_skill").mkdir()           # no skill.md inside
    (root / "also_not").mkdir()
    assert _list_skills(root) == []


def test_list_skills_returns_names_of_dirs_with_skill_md(tmp_path: Path) -> None:
    """Tier 2: dirs that contain skill.md are returned as skill names."""
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("eval", "direct_llm"):
        d = root / name
        d.mkdir()
        (d / "skill.md").write_text("# skill")
    result = _list_skills(root)
    assert "eval" in result
    assert "direct_llm" in result


def test_list_skills_returns_sorted_names(tmp_path: Path) -> None:
    """Tier 2: names are returned in lexicographic sort order."""
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("zebra_skill", "alpha_skill", "middle_skill"):
        d = root / name
        d.mkdir()
        (d / "skill.md").write_text("# skill")
    result = _list_skills(root)
    assert result == sorted(result)
    assert result == ["alpha_skill", "middle_skill", "zebra_skill"]


def test_list_skills_mixed_skips_non_skill_dirs(tmp_path: Path) -> None:
    """Tier 2: only dirs with skill.md are counted; others are filtered out."""
    root = tmp_path / "skills"
    root.mkdir()
    good = root / "real_skill"
    good.mkdir()
    (good / "skill.md").write_text("# skill")
    bad = root / "not_skill"
    bad.mkdir()
    # no skill.md in bad
    result = _list_skills(root)
    assert result == ["real_skill"]


def test_list_skills_file_named_skill_md_at_root_is_not_counted(tmp_path: Path) -> None:
    """Tier 2: a skill.md placed directly under root (not inside a subdir) is ignored."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "skill.md").write_text("# not a skill dir")
    # no subdirs → should still be empty
    assert _list_skills(root) == []
