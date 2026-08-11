"""Tier 2: `reyn agent list` hides archived agents by default (#1954).

Dogfood-found: `_cmd_list` iterated the on-disk agents dir and never consulted
the archive marker, so archived agents stayed visible — inconsistent with where
archived agents are excluded (delegation routing, A2A, the TUI Agents tab, all
via ``list_active_names``) and with the documented intent. These pin: default
hides archived; ``--all`` reveals them marked.

No mocks — a real ``AgentRegistry`` creates + archives agents on disk; the public
``_cmd_list`` is exercised via ``capsys``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.cli.commands.agent import _cmd_list  # noqa: E402
from reyn.runtime.registry import AgentRegistry  # noqa: E402


def _no_factory(profile):
    raise RuntimeError("session factory not used for a read-only list")


def _setup_with_archived(tmp_path: Path) -> None:
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    reg.create("alpha", role="coordinator")
    reg.create("beta", role="worker")
    reg.remove("beta")  # archive (soft-delete) — writes the .archived marker


def test_list_hides_archived_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    """Tier 2: an archived agent is absent from the default `reyn agent list`."""
    _setup_with_archived(tmp_path)
    monkeypatch.chdir(tmp_path)

    _cmd_list(argparse.Namespace(all=False))

    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" not in out  # archived → hidden by default


def test_list_all_shows_archived_marked(tmp_path: Path, monkeypatch, capsys) -> None:
    """Tier 2: `reyn agent list --all` includes archived agents, marked '(archived)'."""
    _setup_with_archived(tmp_path)
    monkeypatch.chdir(tmp_path)

    _cmd_list(argparse.Namespace(all=True))

    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta (archived)" in out


def test_list_from_a_subdirectory_still_finds_the_project_agents(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Tier 2: #4204 bucket A — `reyn agent list` launched from a subdirectory
    of the project must still resolve `.reyn/agents/` at the PROJECT ROOT,
    not create/read a phantom `.reyn/agents/` under the subdirectory. Every
    other CLI command module already converges on
    `_find_project_root(Path.cwd()) or Path.cwd()`; this module previously
    anchored directly on raw `Path.cwd()` instead — a real defect, not just
    a docstring claim.

    Falsify-worthy shape: without the fix, this test's `_cmd_list` call
    would print the "no agents yet" message (it would look under
    `<subdir>/.reyn/agents/`, which is empty/absent) instead of listing the
    real agent created at the project root."""
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    reg.create("alpha", role="coordinator")

    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    _cmd_list(argparse.Namespace(all=False))

    out = capsys.readouterr().out
    assert "alpha" in out
    assert not (subdir / ".reyn" / "agents").exists(), (
        "must not create a phantom .reyn/agents/ under the subdirectory"
    )
