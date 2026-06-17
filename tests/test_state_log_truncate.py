"""Tests for StateLog.truncate_below — the WAL rewrite primitive.

Tier 2: OS invariant — WAL truncation must preserve seq monotonicity across
process restarts and never lose entries that any agent has not yet absorbed.
The truncation is the basis for bounded WAL size on long-running skills
(skill resume design, PR-state-foundation).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.core.events.state_log import StateLog


def _read_all(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _seq_set(entries: list[dict]) -> set[int]:
    return {e["seq"] for e in entries if isinstance(e.get("seq"), int)}


def test_truncate_drops_below_min_keep_seq(tmp_path):
    """Tier 2: entries with seq < min_keep_seq are dropped, others preserved verbatim."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def setup_and_truncate():
        for i in range(1, 11):
            await log.append("inbox_put", target=f"agent_{i}", payload={"i": i})
        # Drop seq 1..4, keep 5..10
        return await log.truncate_below(5)

    stats = asyncio.run(setup_and_truncate())

    assert stats["dropped"] == 4
    assert stats["kept"] == 6
    assert stats["min_kept_seq"] == 5
    assert stats["max_kept_seq"] == 10

    surviving = _read_all(log.path)
    assert _seq_set(surviving) == {5, 6, 7, 8, 9, 10}


def test_truncate_noop_when_min_keep_seq_le_one(tmp_path):
    """Tier 2: min_keep_seq <= 1 is a no-op (everything kept)."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        for i in range(1, 6):
            await log.append("inbox_put", target=f"a{i}", payload={})
        return await log.truncate_below(1), await log.truncate_below(0)

    (s1, s0) = asyncio.run(go())
    assert s1 == {"dropped": 0, "kept": 0, "min_kept_seq": None, "max_kept_seq": None}
    assert s0 == {"dropped": 0, "kept": 0, "min_kept_seq": None, "max_kept_seq": None}
    assert _seq_set(_read_all(log.path)) == {1, 2, 3, 4, 5}


def test_truncate_preserves_highest_seq_as_watermark(tmp_path):
    """Tier 2: even when min_keep_seq > all existing seqs, the highest entry is kept.

    Otherwise next-startup _scan_max_seq() would reset the counter to 0 and
    re-issue already-used seqs into the audit log.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def go():
        for i in range(1, 6):
            await log.append("inbox_put", target=f"a{i}", payload={})
        # Caller asks to drop everything (min_keep_seq beyond all existing seqs).
        return await log.truncate_below(100)

    stats = asyncio.run(go())

    surviving = _read_all(log.path)
    # Watermark — the highest existing seq (5) must remain.
    assert _seq_set(surviving) == {5}
    assert stats["kept"] == 1
    assert stats["max_kept_seq"] == 5


def test_truncate_counter_survives_restart(tmp_path):
    """Tier 2: after truncation, a new StateLog instance issues seqs strictly above max-on-disk.

    Pivotal invariant for replay-correctness: dropped seqs must never be re-issued.
    """
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go1():
        for i in range(1, 11):
            await log.append("inbox_put", target=f"a{i}", payload={})
        # Drop 1..7, keep 8..10
        await log.truncate_below(8)

    asyncio.run(go1())

    # Simulate process restart by constructing a new StateLog over the same file.
    log2 = StateLog(path)

    async def go2():
        return await log2.append("chain_register", target="agent_x")

    new_seq = asyncio.run(go2())
    # Counter must be > max kept seq (10), not reset to 0.
    assert new_seq == 11

    final = _read_all(path)
    assert _seq_set(final) == {8, 9, 10, 11}


def test_truncate_atomic_no_partial_state_on_disk(tmp_path):
    """Tier 2: post-truncate file is well-formed; tmp file is removed.

    rename(tmp, dst) is the atomic boundary; we observe the post-state.
    """
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 6):
            await log.append("inbox_put", target=f"a{i}", payload={})
        await log.truncate_below(3)

    asyncio.run(go())

    assert path.is_file()
    assert not (tmp_path / "wal.jsonl.tmp").exists()
    # All surviving lines parse cleanly — no half-written entries.
    survivors = _read_all(path)
    assert all("seq" in e and "kind" in e for e in survivors)


def test_truncate_skips_torn_lines(tmp_path):
    """Tier 2: a corrupt/torn line in the source file is dropped on rewrite, not propagated."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def setup():
        for i in range(1, 4):
            await log.append("inbox_put", target=f"a{i}", payload={})

    asyncio.run(setup())

    # Inject a torn fragment between valid lines.
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 9, "kind": "in')  # torn — no closing brace, no newline

    async def truncate():
        return await log.truncate_below(2)

    stats = asyncio.run(truncate())
    survivors = _read_all(path)
    # Surviving valid entries: seq 2, 3 (seq 1 dropped, torn line dropped)
    assert _seq_set(survivors) == {2, 3}
    # `dropped` counts both legitimate drops AND torn fragments.
    assert stats["dropped"] >= 1


def test_truncate_on_missing_file_is_noop(tmp_path):
    """Tier 2: truncating a non-existent WAL file returns zero-stats without error."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        return await log.truncate_below(5)

    stats = asyncio.run(go())
    assert stats == {"dropped": 0, "kept": 0,
                     "min_kept_seq": None, "max_kept_seq": None}


def test_truncate_then_iter_from_returns_only_survivors(tmp_path):
    """Tier 2: iter_from after truncation does not surface dropped entries."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 8):
            await log.append("inbox_put", target=f"a{i}", payload={"i": i})
        await log.truncate_below(4)

    asyncio.run(go())
    seen = list(log.iter_from(0))
    assert {e["seq"] for e in seen} == {4, 5, 6, 7}
