"""Tier 2: #5939 PR-4 — `read_history_tail_with_byte_budget`, the
hydration mini-ladder's own "stop_reading_early" step. Real file I/O
throughout (`tmp_path`), never a mock of the filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.runtime.history_tail_reader import read_history_tail_with_byte_budget


def _write_lines(path: Path, rows: "list[dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _row(seq: int, *, role: str = "user", body: str = "x") -> dict:
    return {"seq": seq, "role": role, "content": body}


def test_missing_file_returns_empty_not_truncated(tmp_path: Path):
    """Tier 2: matches read_history_tail's own missing-file contract."""
    lines, truncated_unsafe = read_history_tail_with_byte_budget(
        tmp_path / "nope.jsonl", max_bytes=1000,
    )
    assert lines == []
    assert truncated_unsafe is False


def test_all_lines_fit_under_budget_none_truncated(tmp_path: Path):
    """Tier 2: a small file well under the byte budget -- every line
    returned, in FILE (oldest-first) order, never truncated."""
    path = tmp_path / "history.jsonl"
    rows = [_row(i) for i in range(1, 6)]
    _write_lines(path, rows)

    lines, truncated_unsafe = read_history_tail_with_byte_budget(
        path, min_lines=200, max_bytes=1_000_000,
    )

    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3, 4, 5]
    assert truncated_unsafe is False


def test_byte_budget_stops_early_before_min_lines_and_marks_unsafe(tmp_path: Path):
    """Tier 2: accept -- many lines, no summary anywhere, a byte budget
    small enough to cross well before `min_lines` lines are collected.
    The read stops (fewer than `min_lines` returned) and reports
    `truncated_unsafe=True` (no summary was ever found).

    Strip: change `if cumulative_bytes >= max_bytes: break` to `pass`
    (drop the byte-budget stop entirely) -- this goes RED (all 500 lines
    returned, performed during review)."""
    path = tmp_path / "history.jsonl"
    rows = [_row(i, body="x" * 200) for i in range(1, 501)]  # no summary
    _write_lines(path, rows)

    lines, truncated_unsafe = read_history_tail_with_byte_budget(
        path, min_lines=200, max_bytes=2_000,  # far below what 500 lines need
    )

    loaded_seqs = {json.loads(line)["seq"] for line in lines}
    assert 1 not in loaded_seqs, "the OLDEST entry must not have been reached"
    assert 500 in loaded_seqs, "the NEWEST entry must still be present (backward read)"
    assert truncated_unsafe is True


def test_finding_a_summary_before_budget_exhausted_is_not_unsafe(tmp_path: Path):
    """Tier 2: deny side -- a summary IS found within budget (even if the
    scrollback floor beyond it gets cut short by the SAME byte budget) ->
    `truncated_unsafe=False`, the SAME safe shape `read_history_tail`
    itself already allows for a plain min_lines-after-summary cut."""
    path = tmp_path / "history.jsonl"
    # FILE order is oldest-first; a backward read starts from the END, so
    # the summary must be placed NEAR THE END of the file to be found
    # quickly by a backward read (the realistic "recent compaction"
    # shape -- the most recent summary is always the newest role=summary
    # row).
    rows = [_row(i, body="x" * 50) for i in range(1, 98)]
    rows += [_row(98, role="summary", body="prior summary"), _row(99)]
    _write_lines(path, rows)

    lines, truncated_unsafe = read_history_tail_with_byte_budget(
        path, min_lines=5, max_bytes=500,  # small budget, but summary is near the end
    )

    assert any(json.loads(line).get("role") == "summary" for line in lines)
    assert truncated_unsafe is False


def test_stopping_after_min_lines_and_summary_is_unaffected_by_byte_budget(tmp_path: Path):
    """Tier 2: FP gate -- a generous byte budget that never actually
    triggers behaves byte-identically to `read_history_tail` (min_lines +
    seen_summary is still the ordinary stop condition)."""
    path = tmp_path / "history.jsonl"
    rows = [_row(1, role="summary", body="s")]
    rows += [_row(i) for i in range(2, 10)]
    _write_lines(path, rows)

    lines, truncated_unsafe = read_history_tail_with_byte_budget(
        path, min_lines=3, max_bytes=1_000_000_000,
    )

    assert truncated_unsafe is False
    assert any(json.loads(line).get("role") == "summary" for line in lines)
