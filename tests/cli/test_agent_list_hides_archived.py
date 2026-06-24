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

_SRC = Path(__file__).parent.parent.parent / "src"
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
