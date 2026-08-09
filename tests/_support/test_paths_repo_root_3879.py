"""Tier 2: tests/_support/paths.py resolves the repo root by walking up
for a pyproject.toml marker, and raises rather than guessing when none
exists — #3879's fix for the depth-counted-root failure class (111 test
files computed repo-root as Path(__file__).parent.parent-style directory
counting, which silently breaks the moment the file's own depth changes;
confirmed directly on #3989's builtin-bucket move).
"""
from __future__ import annotations

import pytest

from tests._support.paths import REPO_ROOT, _find_repo_root


def test_repo_root_resolves_to_the_real_repo_root() -> None:
    """Tier 2: the witness condition lead-coder asked for — REPO_ROOT
    actually carries pyproject.toml, not merely "some directory that
    didn't raise"."""
    assert (REPO_ROOT / "pyproject.toml").is_file()


def test_resolution_is_depth_independent() -> None:
    """Tier 2: falsification target — a depth-counted root
    (Path(__file__).parent.parent) would resolve to a DIFFERENT directory
    depending on how deep *start* is nested — the whole bug class this
    module exists to close. Build two starting points at different depths
    under the SAME real repo tree and confirm both resolve to the
    identical root, proving the answer does not depend on how many
    directories separate *start* from the root."""
    shallow = REPO_ROOT / "tests"
    deep = REPO_ROOT / "tests" / "_support"
    assert _find_repo_root(shallow) == REPO_ROOT
    assert _find_repo_root(deep) == REPO_ROOT


def test_no_marker_found_raises_instead_of_guessing(tmp_path) -> None:
    """Tier 2: falsification — point the walk at pytest's tmp_path, a
    fresh directory created outside this repo's tree (under the OS temp
    root, which carries no pyproject.toml on any CI runner or dev machine
    this suite runs on). A silent cwd/None fallback would pass this call
    with no error; the raise is the behavior being tested."""
    with pytest.raises(RuntimeError, match="no pyproject.toml found"):
        _find_repo_root(tmp_path)
