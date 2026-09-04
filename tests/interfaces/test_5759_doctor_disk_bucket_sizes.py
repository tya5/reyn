"""Tier 2: #5759 stage 1 (architect + lead-coder assignment, part of
#5759) — ``reyn doctor`` gains real, measured byte counts for the 2
buckets with NO gate of any kind (``state/``, ``cache/``) — visibility
only, no retention decision (that is stage 2).

architect's own census (#5759): ``agents/*/history.jsonl`` and ``events/``
already have their own dedicated doctor sections (real byte counts,
pre-#5759) — this file covers the 2 genuinely new lines this stage adds,
plus the ``memory/`` bucket total (broader than the ``history-content/``
subset the existing ``tool-results:`` row already reports).

No mocks — drives the real ``run`` against a real on-disk ``.reyn/`` tree
under ``tmp_path``, matching this command family's own established shape
(``test_4364_storage_cap_doctor_row.py``, ``test_4364_pr3a_doctor_cli.py``).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor import _dir_size_bytes, run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    return tmp_path


def test_state_bucket_reports_real_bytes(project: Path, capsys) -> None:
    """Tier 2: strip-falsifier target — a real file written under
    ``.reyn/state/`` shows up in the printed row with its GENUINE byte
    count, not a placeholder or a config-declared value (D-1's own
    "measure, don't assert" rule this whole module follows)."""
    state_dir = project / ".reyn" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "wal.jsonl").write_text("x" * 543, encoding="utf-8")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out

    assert "state/:   1 file(s), 543 bytes" in out


def test_cache_bucket_reports_real_bytes_across_nested_dirs(
    project: Path, capsys,
) -> None:
    """Tier 2: ``cache/`` is measured recursively (``index/<source>/``
    nesting, matching the real layout ``reyn-dir-layout.md`` documents),
    not just its own direct children."""
    cache_dir = project / ".reyn" / "cache" / "index" / "mysource"
    cache_dir.mkdir(parents=True)
    (cache_dir / "data.sqlite").write_text("y" * 321, encoding="utf-8")
    (project / ".reyn" / "cache" / "registry-cache" ).mkdir(parents=True)
    (project / ".reyn" / "cache" / "registry-cache" / "r.json").write_text(
        "z" * 100, encoding="utf-8",
    )

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out

    assert "cache/:   2 file(s), 421 bytes" in out


def test_memory_bucket_total_includes_history_content(
    project: Path, capsys,
) -> None:
    """Tier 2: ``memory/``'s own row is the FULL bucket total, including
    ``history-content/`` — broader than the pre-existing ``tool-results:``
    row (which reports that one subtree only, under its own legacy
    field name). Both a memory-only file AND a history-content file must
    count toward this row."""
    memory_dir = project / ".reyn" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "notes.md").write_text("a" * 111, encoding="utf-8")
    hc_dir = memory_dir / "history-content" / "default" / "s1"
    hc_dir.mkdir(parents=True)
    (hc_dir / "spill.txt").write_text("b" * 222, encoding="utf-8")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out

    assert "memory/:  2 file(s), 333 bytes" in out


def test_missing_buckets_report_zero_not_a_crash(project: Path, capsys) -> None:
    """Tier 2: a project with none of state/memory/cache written yet
    (a fresh install) must not raise — D-1/D-2's own "report, never
    fabricate, never crash" posture, matching ``_events_dir_stats``'s
    identical no-directory-yet handling."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out

    assert "state/:   0 file(s), 0 bytes" in out
    assert "memory/:  0 file(s), 0 bytes" in out
    assert "cache/:   0 file(s), 0 bytes" in out


def test_dir_size_bytes_helper_directly(tmp_path: Path) -> None:
    """Tier 2: the shared helper itself, driven directly — a nested
    file counts, a directory entry does not, and a fully-missing root
    returns (0, 0) rather than raising."""
    assert _dir_size_bytes(tmp_path / "does-not-exist") == (0, 0)

    root = tmp_path / "bucket"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("12345", encoding="utf-8")
    (root / "b.txt").write_text("123", encoding="utf-8")

    assert _dir_size_bytes(root) == (2, 8)
