"""Tier 2: #5759 stage 2 Phase 2 — `rewrite_history_dropping`, the first
real deletion path `history.jsonl` has ever had.

Real on-disk file under `tmp_path`, no mocks. Mirrors
`StateLog.truncate_below`'s own atomic strategy (stream-read -> `.tmp` ->
fsync -> rename) but a caller-supplied predicate decides survivors, since
this GC drops a bounded MIDDLE range, not a `seq < floor` prefix.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.runtime.history_tail_reader import rewrite_history_dropping


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _entry(seq: int, role: str = "user") -> str:
    return json.dumps({"role": role, "seq": seq, "text": f"t{seq}"})


def _seqs(path: Path) -> list[int]:
    return [
        json.loads(line)["seq"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_missing_file_is_a_no_op(tmp_path: Path) -> None:
    """Tier 2: a history.jsonl that doesn't exist yet -- no crash, zero
    counts, nothing to rewrite (matches StateLog._do_truncate's own
    missing-file no-op)."""
    path = tmp_path / "history.jsonl"

    stats = rewrite_history_dropping(path, should_drop=lambda e: True)

    assert stats == {"dropped": 0, "kept": 0}
    assert not path.exists()


def test_drops_only_the_middle_range_head_and_tail_survive(tmp_path: Path) -> None:
    """Tier 2: strip-falsifier target. seq 1-3 (head), 4-6 (folded range,
    dropped), 7-9 (unfolded tail) -- a bounded middle range, unlike a
    simple prefix truncation, both ends survive untouched."""
    path = tmp_path / "history.jsonl"
    _write(path, [_entry(i) for i in range(1, 10)])

    stats = rewrite_history_dropping(
        path, should_drop=lambda e: 4 <= e["seq"] <= 6,
    )

    assert stats == {"dropped": 3, "kept": 6}
    assert _seqs(path) == [1, 2, 3, 7, 8, 9]


def test_malformed_and_blank_lines_are_never_dropped(tmp_path: Path) -> None:
    """Tier 2: fail-closed on anything the predicate cannot see -- a torn
    fragment, a non-dict JSON value, and a missing/non-int seq are all
    written through unchanged, never counted as dropped. Opposite default
    from StateLog._do_truncate's own WAL torn-fragment handling
    (deliberate: history.jsonl is user-facing scrollback, not a replay
    log -- unknown content is not proven garbage)."""
    path = tmp_path / "history.jsonl"
    lines = [
        _entry(1),
        "not json at all {{{",
        json.dumps(["a", "list", "not", "a", "dict"]),
        json.dumps({"role": "user", "text": "no seq field"}),
        "",
        _entry(2),
    ]
    _write(path, lines)

    stats = rewrite_history_dropping(path, should_drop=lambda e: True)

    # Only the 2 well-formed {"seq": int} entries were even eligible; both
    # were dropped since the predicate always returns True for them.
    assert stats == {"dropped": 2, "kept": 0}
    survivors = path.read_text(encoding="utf-8").splitlines()
    assert "not json at all {{{" in survivors
    assert json.dumps(["a", "list", "not", "a", "dict"]) in survivors
    assert json.dumps({"role": "user", "text": "no seq field"}) in survivors


def test_atomic_rename_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    """Tier 2: the .tmp sibling used mid-rewrite does not survive a
    successful rewrite -- `Path.replace` renamed it over the original,
    matching StateLog._do_truncate's own atomic-rename contract."""
    path = tmp_path / "history.jsonl"
    _write(path, [_entry(1), _entry(2)])

    rewrite_history_dropping(path, should_drop=lambda e: e["seq"] == 1)

    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert _seqs(path) == [2]


def test_dropping_nothing_leaves_file_content_unchanged(tmp_path: Path) -> None:
    """Tier 2: a predicate that never matches still performs the full
    rewrite (this function has no early-return "nothing to do" shortcut,
    unlike StateLog.truncate_below's min_keep_seq<=1 case) -- content
    must come out byte-identical in seq terms regardless."""
    path = tmp_path / "history.jsonl"
    _write(path, [_entry(i) for i in range(1, 4)])

    stats = rewrite_history_dropping(path, should_drop=lambda e: False)

    assert stats == {"dropped": 0, "kept": 3}
    assert _seqs(path) == [1, 2, 3]
