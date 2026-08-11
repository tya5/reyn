"""Tier 2: #4204 bucket A — dogfood.py's storage dir anchors on the project
root, not raw cwd.

`_dogfood_base_dir()` previously returned `Path.cwd() / ".reyn" / "dogfood"`
directly — a real defect, not just a docstring claim: the SAME module's own
`:475` (the `run` scenario-dispatch path) already correctly uses
`_find_project_root(Path.cwd()) or Path.cwd()`, so this wasn't even an
internally consistent convention within the file. `reyn dogfood` launched
from a subdirectory of the project silently read/wrote runs + baselines
under a phantom `.reyn/dogfood/` instead of the real project's.

`_runs_dir()`/`_baselines_dir()` both delegate to `_dogfood_base_dir()`, so
this one fix covers both.
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.cli.commands.dogfood import (
    _baselines_dir,
    _dogfood_base_dir,
    _runs_dir,
)


def test_base_dir_resolves_to_project_root_from_a_subdirectory(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: launched from a subdirectory, the dogfood storage dir still
    anchors on the project root (the `reyn.yaml` ancestor), not the
    subdirectory."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    assert _dogfood_base_dir() == tmp_path / ".reyn" / "dogfood"
    assert not (subdir / ".reyn").exists()


def test_runs_and_baselines_dirs_share_the_same_fix(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: both derived dirs delegate to _dogfood_base_dir(), so they
    inherit the fix — a real witness that the delegation wasn't
    shadowed/duplicated somewhere."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    assert _runs_dir() == tmp_path / ".reyn" / "dogfood" / "runs"
    assert _baselines_dir() == tmp_path / ".reyn" / "dogfood" / "baselines"
