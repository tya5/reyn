"""#3716 — `reyn memory` CLI project-root resolution.

Deferred from #3705's Session-write-path fix: `reyn memory` had NO
project-root resolution anywhere in its own call chain — `memory_dir()`
(`data/memory/memory_paths.py`) took no root argument at all, so every
write/read silently followed the ambient process cwd with no way for a
caller to override it (unlike `default_snapshot_path`, whose `root=` param
#3705 added — a value the caller could supply but the callee ignored;
`reyn memory` had no value to supply in the first place).

`memory_dir()` gains a `root=` param (same convention as
`default_snapshot_path`), and `interfaces/cli/commands/memory.py` gains its
own project-root resolution (`_project_reyn_root`, mirroring `reyn chat`'s
`_find_project_root(Path.cwd()) or Path.cwd()`), threaded through
`_layer_dir` — the CLI's single seam every subcommand already goes through.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.data.memory.memory_paths import memory_dir
from reyn.interfaces.cli.commands.memory import _layer_dir, _project_reyn_root


def test_memory_dir_respects_an_explicit_root():
    """Tier 2: #3716 — `memory_dir(root=...)` is anchored on the given root,
    not the ambient cwd."""
    root = Path("/some/project/.reyn")
    assert memory_dir(agent=None, root=root) == root / "memory"
    assert memory_dir(agent="alpha", root=root) == root / "agents" / "alpha" / "memory"


def test_memory_dir_falls_back_to_cwd_when_root_is_none(tmp_path, monkeypatch):
    """Tier 2: regression guard — `root=None` (the default) is byte-identical
    to the pre-#3716 behavior."""
    monkeypatch.chdir(tmp_path)
    assert memory_dir(agent=None) == tmp_path / ".reyn" / "memory"


def test_project_reyn_root_walks_up_to_the_real_project_root(tmp_path, monkeypatch):
    """Tier 2: #3716 — `_project_reyn_root()` finds the SAME project root
    `reyn chat` would, walking UP from a subdirectory the operator happens
    to be standing in — not just the immediate cwd. This is what makes
    `reyn memory list` (run from a project subdirectory, the normal way any
    reyn CLI command is invoked) resolve to the project's real `.reyn/`,
    not fabricate one under the subdirectory."""
    project_root = tmp_path / "my-project"
    (project_root / "src" / "deep" / "subdir").mkdir(parents=True)
    (project_root / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    monkeypatch.chdir(project_root / "src" / "deep" / "subdir")

    assert _project_reyn_root() == project_root / ".reyn"


def test_layer_dir_uses_the_project_root_not_raw_cwd(tmp_path, monkeypatch):
    """Tier 2: #3716 core witness — `_layer_dir(args)` (every `reyn memory`
    subcommand's single directory-resolution seam) resolves under the
    PROJECT root, not wherever the process happens to be standing when
    invoked from a subdirectory."""
    project_root = tmp_path / "my-project"
    subdir = project_root / "some" / "nested" / "cwd"
    subdir.mkdir(parents=True)
    (project_root / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    monkeypatch.chdir(subdir)

    args = argparse.Namespace(agent=None)
    assert _layer_dir(args) == project_root / ".reyn" / "memory"
    # NOT resolved against the subdirectory the process happened to start in.
    assert _layer_dir(args) != subdir / ".reyn" / "memory"
