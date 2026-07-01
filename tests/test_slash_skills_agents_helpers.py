"""Tier 2: slash/skills._list_skills + slash/agents._attach_completer contracts.

_list_skills: pure filesystem scan — returns sorted skill names under a root
directory.  A skill directory is any subdirectory that contains a `skill.md`
file.  Tests exercise: missing root, empty root, dirs without skill.md, dirs
with skill.md, and sort order.

_attach_completer: session-aware tab-completion helper — returns [] when the
session has no registry, otherwise delegates to registry.list_active_names().
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.interfaces.slash.agents import _attach_completer
from reyn.interfaces.slash.skills import _list_skills

# ── _list_skills ─────────────────────────────────────────────────────────────


def test_list_skills_nonexistent_root_returns_empty(tmp_path: pytest.fixture) -> None:
    """Tier 2: non-existent root directory → []."""
    result = _list_skills(tmp_path / "no_such_dir")
    assert result == []


def test_list_skills_empty_root_returns_empty(tmp_path: pytest.fixture) -> None:
    """Tier 2: existing but empty root directory → []."""
    root = tmp_path / "skills"
    root.mkdir()
    result = _list_skills(root)
    assert result == []


def test_list_skills_dir_without_skill_md_excluded(tmp_path: pytest.fixture) -> None:
    """Tier 2: subdirectory without skill.md is excluded from results."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "orphan").mkdir()  # no skill.md inside
    result = _list_skills(root)
    assert result == []


def test_list_skills_single_skill_returned(tmp_path: pytest.fixture) -> None:
    """Tier 2: one skill dir with skill.md → list containing that skill name."""
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = root / "eval"
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text("# eval")
    result = _list_skills(root)
    assert "eval" in result


def test_list_skills_multiple_skills_sorted(tmp_path: pytest.fixture) -> None:
    """Tier 2: multiple skill dirs → names sorted alphabetically."""
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("zebra", "apple", "mango"):
        d = root / name
        d.mkdir()
        (d / "skill.md").write_text(f"# {name}")
    result = _list_skills(root)
    assert result == ["apple", "mango", "zebra"]


def test_list_skills_file_named_skill_md_at_root_not_included(tmp_path: pytest.fixture) -> None:
    """Tier 2: skill.md placed directly under root (not in a subdir) is ignored."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "skill.md").write_text("# stray")  # not inside a skill dir
    result = _list_skills(root)
    assert result == []


def test_list_skills_mixed_dirs_only_valid_included(tmp_path: pytest.fixture) -> None:
    """Tier 2: mix of valid and invalid dirs — only those with skill.md included."""
    root = tmp_path / "skills"
    root.mkdir()
    valid = root / "good_skill"
    valid.mkdir()
    (valid / "skill.md").write_text("# good")
    bad = root / "no_skill_here"
    bad.mkdir()
    # no skill.md under bad/
    result = _list_skills(root)
    assert result == ["good_skill"]


# ── _attach_completer ─────────────────────────────────────────────────────────


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_active_names(self) -> list[str]:
        return list(self._names)


def test_attach_completer_no_registry_returns_empty() -> None:
    """Tier 2: session._registry is None → _attach_completer returns []."""
    session = SimpleNamespace(_registry=None)
    result = _attach_completer(session)
    assert result == []


def test_attach_completer_no_registry_attr_returns_empty() -> None:
    """Tier 2: session has no _registry attribute at all → returns []."""
    session = SimpleNamespace()  # no _registry key
    result = _attach_completer(session)
    assert result == []


def test_attach_completer_delegates_to_registry() -> None:
    """Tier 2: session._registry present → delegates to list_active_names()."""
    session = SimpleNamespace(_registry=_FakeRegistry(["alpha", "beta"]))
    result = _attach_completer(session)
    assert "alpha" in result
    assert "beta" in result


def test_attach_completer_arg_partial_is_ignored() -> None:
    """Tier 2: arg_partial argument does not affect result (compat shim, unused)."""
    session = SimpleNamespace(_registry=_FakeRegistry(["x"]))
    assert _attach_completer(session, "xy") == _attach_completer(session, "")
