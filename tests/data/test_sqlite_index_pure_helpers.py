"""Tier 2: data/index/backends/sqlite.py pure helper contracts.

_db_path(workspace_root, source) constructs the canonical path to the
SQLite index database file under the workspace's .reyn cache directory.

_within_paths(path, roots) checks whether a filesystem path is one of the
given root paths or a descendant, using resolve() for canonical comparison.
Used as the sandbox write-paths gate for host-direct index writes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.data.index.backends.sqlite import _db_path, _within_paths

# ── _db_path ──────────────────────────────────────────────────────────────────


def test_db_path_canonical_structure(tmp_path: Path) -> None:
    """Tier 2: db path ends with .reyn/cache/index/<source>/index.db."""
    result = _db_path(tmp_path, "my_source")
    assert result == tmp_path / ".reyn" / "cache" / "index" / "my_source" / "index.db"


def test_db_path_uses_source_name(tmp_path: Path) -> None:
    """Tier 2: the source name is used as the penultimate directory segment."""
    result = _db_path(tmp_path, "code_search")
    assert result.parent.name == "code_search"


def test_db_path_filename_is_index_db(tmp_path: Path) -> None:
    """Tier 2: the leaf filename is always 'index.db'."""
    assert _db_path(tmp_path, "anything").name == "index.db"


def test_db_path_different_sources_give_different_paths(tmp_path: Path) -> None:
    """Tier 2: different source names produce distinct paths."""
    assert _db_path(tmp_path, "src_a") != _db_path(tmp_path, "src_b")


# ── _within_paths ─────────────────────────────────────────────────────────────


def test_within_paths_exact_match(tmp_path: Path) -> None:
    """Tier 2: path equal to a root → True."""
    assert _within_paths(tmp_path, [str(tmp_path)]) is True


def test_within_paths_descendant(tmp_path: Path) -> None:
    """Tier 2: path nested under a root → True."""
    child = tmp_path / "subdir" / "file.txt"
    child.parent.mkdir(parents=True)
    assert _within_paths(child, [str(tmp_path)]) is True


def test_within_paths_sibling_outside_root(tmp_path: Path) -> None:
    """Tier 2: path outside all roots → False."""
    other = tmp_path.parent / "other"
    assert _within_paths(other, [str(tmp_path)]) is False


def test_within_paths_empty_roots_returns_false(tmp_path: Path) -> None:
    """Tier 2: empty roots list → False (no allowed roots means nothing is within)."""
    assert _within_paths(tmp_path, []) is False


def test_within_paths_one_of_many_roots_matches(tmp_path: Path) -> None:
    """Tier 2: path under any one of multiple roots → True."""
    child = tmp_path / "file.txt"
    other_root = str(tmp_path.parent / "unrelated")
    assert _within_paths(child, [other_root, str(tmp_path)]) is True


def test_within_paths_accepts_path_object(tmp_path: Path) -> None:
    """Tier 2: path argument can be a Path object (not just a string)."""
    assert _within_paths(tmp_path, [str(tmp_path)]) is True
