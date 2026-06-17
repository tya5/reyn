"""Tier 2: #1375 D10 — glob returns files only, filtered in the backend.

D10 was a general OS bug: ``EnvironmentBackend.glob`` returned files AND
directories and the Workspace filtered to files host-side with
``Path(m).is_file()``. Under the container backend that host-side filter stats
*container* paths against the *host* filesystem (where they do not exist), so
every match was dropped and glob returned nothing in-container.

The fix narrows the backend ``glob`` contract to "files only" and moves the
file-filter into each backend's own environment (symmetric with ``grep``). The
host path is behaviour-preserving (same filesystem); the container path is
proven by the faithful in-container re-smoke (no container is faked in a unit —
see [[feedback_fake_backend_unit_misses_real_integration]]).

This file pins the moved contract on the surface a unit CAN exercise:

  (a) HostBackend.glob excludes directories that match the pattern — the
      files-only contract that used to live in the caller now lives in the
      backend (both the relative-root and absolute-recursive branches);
  (b) Workspace.glob_files returns only files end-to-end, and a directory whose
      name matches the pattern does not occupy a max_results slot (the
      leading-directory truncation the old host-side filter guarded against is
      now guaranteed by construction).

No mocks: real HostBackend / Workspace instances, public surfaces only.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.environment.host_backend import HostBackend


def test_host_backend_glob_relative_excludes_directories(tmp_path: Path) -> None:
    """Tier 2: relative-root glob returns files only, not matching dirs."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "sub").mkdir()  # a directory matching `pkg/*`
    (tmp_path / "pkg" / "sub" / "deep.py").write_text("y = 2\n")

    matches = HostBackend().glob("pkg/*", root=tmp_path)
    names = {p.name for p in matches}
    assert names == {"mod.py"}, names  # `sub` (a dir) is excluded
    assert all(p.is_file() for p in matches)


def test_host_backend_glob_absolute_excludes_directories(tmp_path: Path) -> None:
    """Tier 2: absolute recursive glob (root=None) returns files only."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.py").write_text("x = 1\n")
    (tmp_path / "a" / "d").mkdir()  # a directory matched by **/*

    matches = HostBackend().glob(str(tmp_path / "**" / "*"))
    assert matches, "expected at least the file to match"
    assert all(p.is_file() for p in matches)
    assert (tmp_path / "a" / "d") not in matches  # the dir is excluded
    assert (tmp_path / "a" / "f.py") in matches


def test_glob_files_returns_files_only(tmp_path: Path) -> None:
    """Tier 2: Workspace.glob_files excludes a dir whose name matches."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    (tmp_path / "foo.txt").write_text("hi\n")
    (tmp_path / "foo_dir").mkdir()  # name matches `foo*` but is a directory

    assert ws.glob_files("foo*") == ["foo.txt"]


def test_glob_files_directory_does_not_consume_max_results_slot(tmp_path: Path) -> None:
    """Tier 2: leading dirs never truncate the file list (now by construction).

    The old host-side filter existed because a glob whose first matches were
    directories could truncate the file list to ~zero under max_results. With
    the backend pre-filtering to files, the one file surfaces even when many
    matching directories sort ahead of it and max_results is tight.
    """
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    for i in range(5):
        (tmp_path / f"item_{i}_dir").mkdir()  # 5 dirs sort before the file
    (tmp_path / "item_z.txt").write_text("payload\n")

    assert ws.glob_files("item_*", max_results=1) == ["item_z.txt"]


def test_glob_files_matches_empty_file_by_name(tmp_path: Path) -> None:
    """Tier 2: #1375 D7 — glob matches an EMPTY (0-byte) file by name.

    The swe_bench D7 filename-finding (explore/plan preprocessors) reverted from
    a grep-with-glob workaround to the real glob op once this D10 fix landed. The
    distinguishing property the revert relies on: glob matches by NAME, so a
    0-byte file (e.g. an empty package ``__init__.py``) is found — the
    grep-with-glob workaround required a non-empty line and silently missed it.
    """
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")  # 0-byte, name-only match

    assert ws.glob_files("**/__init__.py") == ["pkg/__init__.py"]
