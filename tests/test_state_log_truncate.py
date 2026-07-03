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
        await log.truncate_below(5)
        await log.flush()
        return log.last_truncate_stats

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
        # min_keep_seq <= 1 is a no-op → last_truncate_stats set synchronously (no worker).
        await log.truncate_below(1)
        s1 = log.last_truncate_stats
        await log.truncate_below(0)
        s0 = log.last_truncate_stats
        return s1, s0

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
        await log.truncate_below(100)
        await log.flush()
        return log.last_truncate_stats

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
        await log.flush()

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
        await log.flush()

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
        await log.truncate_below(2)
        await log.flush()
        return log.last_truncate_stats

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
        await log.truncate_below(5)
        await log.flush()
        return log.last_truncate_stats

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
        await log.flush()

    asyncio.run(go())
    seen = list(log.iter_from(0))
    assert {e["seq"] for e in seen} == {4, 5, 6, 7}


def test_removed_skill_wal_kinds_recovery_safe(tmp_path):
    """Tier 2c: removing the skill_* kinds from WAL_EVENT_KINDS is crash-recovery-safe.

    (a) WRITE side — append REJECTS a removed skill_* kind (0 live writers, so live
        emit is unaffected; a stale/typo writer cannot silently fragment the vocab).
    (b) READ side — iter_from still READS a legacy skill_* WAL line written by a
        pre-removal build. The read path does NOT validate against WAL_EVENT_KINDS,
        so an old on-disk WAL carrying skill_* entries still replays without raising
        or dropping — and, crucially, the KEPT entry that comes AFTER the legacy
        line is not lost.

    FALSIFICATION: had the removal gated the READ path on WAL_EVENT_KINDS too, the
    legacy skill_started line would raise/drop on iter_from and every kept entry
    after it in the same WAL would be lost on recovery (a #2259/#2260-class
    data-loss vector). The reconstruction-side fall-through + snapshot-backed
    truncate-survival (a kept state applied before a snapshot survives WAL
    truncation below its seq, with a WAL-only control) is covered by
    ``test_agent_snapshot::test_truncate_falsify_snapshot_backed_kept_state_survives``.
    """
    wal = tmp_path / "wal.jsonl"
    log = StateLog(wal)

    async def scenario() -> int:
        # (a) WRITE-side: every removed skill_* kind is rejected at append.
        for dead in ("skill_started", "skill_phase_advanced", "skill_completed",
                     "skill_discarded", "skill_resumed"):
            try:
                await log.append(dead, run_id="r1")
            except ValueError as exc:
                assert "unknown WAL event kind" in str(exc)
            else:
                raise AssertionError(f"append({dead!r}) must reject a removed kind")
        s1 = await log.append("step_started", run_id="r1", op="file/read")
        await log.flush()
        return s1

    s1 = asyncio.run(scenario())

    # (b) READ-side: a legacy skill_started line written by a pre-removal build
    #     (bypassing the now-rejecting append), followed by a KEPT entry.
    with wal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": s1 + 1, "kind": "skill_started", "run_id": "r1"}) + "\n")
        f.write(json.dumps({"seq": s1 + 2, "kind": "step_completed", "run_id": "r1"}) + "\n")

    kinds = [e.get("kind") for e in log.iter_from(0)]
    assert "step_started" in kinds       # the appended kept entry reads
    assert "skill_started" in kinds      # legacy removed-kind line reads (no reject/drop)
    assert "step_completed" in kinds     # the KEPT entry AFTER the legacy line is NOT lost
