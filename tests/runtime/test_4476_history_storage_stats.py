"""Tier 2: #4476 Phase 1 — history.jsonl storage measurement
(`history_file_stats`/`aggregate_history_stats`).

Read-only, policy-independent — feeds the SAME "measurement first, owner
decides retention numbers later" order as #4478/#4485 (`reyn media stats`).
No truncation/deletion is introduced anywhere in this file or by the
functions it tests.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.history_tail_reader import (
    aggregate_history_stats,
    history_file_stats,
)


def _write_history(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


# ── history_file_stats (single file) ────────────────────────────────────


def test_missing_file_reports_zero(tmp_path: Path):
    """Tier 2: no history.jsonl yet — (0, 0), not an error."""
    b, lines = history_file_stats(tmp_path / "does-not-exist" / "history.jsonl")
    assert (b, lines) == (0, 0)


def test_counts_bytes_and_nonempty_lines(tmp_path: Path):
    """Tier 2: byte count matches on-disk size exactly; line count skips
    blank lines, matching read_history_after's own "blank isn't an entry"
    rule."""
    hist = tmp_path / "history.jsonl"
    content_lines = [
        '{"seq": 1, "role": "user"}',
        "",  # blank — must not be counted as a turn
        '{"seq": 2, "role": "assistant"}',
        '{"seq": 3, "role": "user"}',
    ]
    _write_history(hist, content_lines)

    b, lines = history_file_stats(hist)
    assert b == hist.stat().st_size
    assert lines == 3


def test_never_writes_to_the_file_it_measures(tmp_path: Path):
    """Tier 2: (accept-side) measuring is a pure read — the file's own
    mtime/content must be unchanged after the call."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    before = hist.read_bytes()

    history_file_stats(hist)
    history_file_stats(hist)

    assert hist.read_bytes() == before


# ── aggregate_history_stats (project-wide) ──────────────────────────────


def test_aggregate_reports_zero_with_no_agents_dir(tmp_path: Path):
    """Tier 2: a fresh project with no .reyn/agents/ yet — all-zero, no
    error, and no directory created as a side effect."""
    stats = aggregate_history_stats(tmp_path)
    assert stats.file_count == 0
    assert stats.total_bytes == 0
    assert stats.total_lines == 0
    assert not (tmp_path / ".reyn").exists()


def test_aggregate_sums_across_multiple_agents(tmp_path: Path):
    """Tier 2: two separate agents' history.jsonl files — totals are the
    sum across both, and file_count reflects both being found."""
    agents = tmp_path / ".reyn" / "agents"
    _write_history(
        agents / "alice" / "history.jsonl",
        ['{"seq": 1}', '{"seq": 2}'],
    )
    _write_history(
        agents / "bob" / "history.jsonl",
        ['{"seq": 1}', '{"seq": 2}', '{"seq": 3}'],
    )

    stats = aggregate_history_stats(tmp_path)
    assert stats.file_count == 2
    assert stats.total_lines == 5
    expected_bytes = (
        (agents / "alice" / "history.jsonl").stat().st_size
        + (agents / "bob" / "history.jsonl").stat().st_size
    )
    assert stats.total_bytes == expected_bytes


def test_aggregate_finds_a_nested_spawned_session_history_too(tmp_path: Path):
    """Tier 2: a spawned sub-session's own history.jsonl (nested under
    agents/<name>/state/sessions/<sid>/, registry.py's own layout) is
    discovered by the recursive glob, not just a top-level agent file."""
    agents = tmp_path / ".reyn" / "agents"
    _write_history(agents / "alice" / "history.jsonl", ['{"seq": 1}'])
    _write_history(
        agents / "alice" / "state" / "sessions" / "sub1" / "history.jsonl",
        ['{"seq": 1}', '{"seq": 2}'],
    )

    stats = aggregate_history_stats(tmp_path)
    assert stats.file_count == 2
    assert stats.total_lines == 3


def test_aggregate_never_writes_anything(tmp_path: Path):
    """Tier 2: (accept-side) calling aggregate_history_stats repeatedly
    does not mutate any discovered file or create new ones."""
    agents = tmp_path / ".reyn" / "agents"
    _write_history(agents / "alice" / "history.jsonl", ['{"seq": 1}'])
    before_names = sorted(p.name for p in agents.rglob("*") if p.is_file())

    aggregate_history_stats(tmp_path)
    aggregate_history_stats(tmp_path)

    after_names = sorted(p.name for p in agents.rglob("*") if p.is_file())
    assert after_names == before_names
