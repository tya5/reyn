"""Tests for StateLog._scan_max_seq's tail-read optimization (#2946 Item 1).

Tier 2: OS invariant — the cold-start seq-watermark recovery (`StateLog.__init__` →
`_scan_max_seq`) must recover EXACTLY the same `max_seq` a full O(WAL) scan would,
via a cheaper O(tail) backward read, including across a WAL truncation that keeps
`always_keep_kinds` entries below the truncation floor (CLAUDE.md Recovery-feature
PR gate: any PR touching WAL-event-derived recovery state needs a truncate-falsify
test — set X, truncate past X's events, reconstruct, assert X survives).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.core.events.snapshot_generations import REWIND_KIND
from reyn.core.events.state_log import StateLog


def test_scan_max_seq_empty_wal_is_zero(tmp_path):
    """Tier 2: no WAL file on disk — current_seq starts at 0 (edge case: empty)."""
    log = StateLog(tmp_path / "wal.jsonl")
    assert log.current_seq == 0


def test_scan_max_seq_matches_full_scan_on_fresh_wal(tmp_path):
    """Tier 2: after a batch of appends (no truncation), a freshly-constructed
    StateLog over the same file recovers the max_seq a full scan would have found
    (the tail-read and full-scan paths must agree here too, not only in the
    truncated-WAL case)."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 51):
            await log.append("inbox_put", target=f"a{i}", payload={"i": i})
        await log.flush()

    asyncio.run(go())

    log2 = StateLog(path)
    assert log2.current_seq == 50


def test_scan_max_seq_survives_truncate_with_always_keep_kinds_below_floor(tmp_path):
    """Tier 2: ★ truncate-falsify (CLAUDE.md Recovery-feature PR gate).

    Hazard (architect, #2946 scope comment): `_do_truncate` preserves the highest
    seq present as a watermark AND keeps `always_keep_kinds` entries (e.g. a
    `rewind` reset-record) below the truncation floor. A naive tail-read that
    special-cases `always_keep_kinds` — e.g. treating them as non-counter-bearing
    "sentinel" records to be skipped when scanning backward for the max seq — is
    fooled: it can walk straight past the true watermark (which may itself be an
    `always_keep_kinds` entry, appended most recently) and under-recover the
    counter, silently re-issuing already-used seqs on the next append.

    Set X: append entries 1..6, with seq=2 an early `rewind` record (kept below
    the floor purely via `always_keep_kinds`) and seq=6 a LATER `rewind` record
    that is also the true watermark (the highest seq in the file, and itself an
    `always_keep_kinds` kind — the case a naive "skip always_keep_kinds" tail-read
    gets wrong).

    Truncate past X's low-seq events (min_keep_seq=5): entries 1,3,4 are dropped;
    entry 2 survives ONLY via always_keep_kinds; entries 5,6 survive on seq alone.

    Reconstruct: a new StateLog over the truncated file must recover max_seq=6 —
    not 2 (the low always_keep_kinds entry) and not some undercount from skipping
    the always_keep_kinds watermark entry.

    (Strip-falsify verified manually: temporarily changing the tail-read to skip
    `kind == "rewind"` entries when scanning backward made this assertion fail
    with `current_seq == 5` instead of `6` — confirming the test actually
    discriminates the hazard, not just the happy path.)
    """
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        await log.append("inbox_put", target="a1", payload={})  # seq 1
        await log.append(REWIND_KIND, target_n=0)  # seq 2 (rewind, below floor)
        await log.append("inbox_put", target="a3", payload={})  # seq 3
        await log.append("inbox_put", target="a4", payload={})  # seq 4
        await log.append("inbox_put", target="a5", payload={})  # seq 5
        await log.append(REWIND_KIND, target_n=1)  # seq 6 (rewind, true watermark)
        await log.flush()
        await log.truncate_below(5, always_keep_kinds=frozenset({REWIND_KIND}))
        await log.flush()
        return log.last_truncate_stats

    stats = asyncio.run(go())

    # Sanity on the truncate itself: 2 survives despite seq<5 (always_keep_kinds);
    # 1,3,4 are dropped; 5,6 survive on seq alone.
    surviving = _read_all(path)
    assert {e["seq"] for e in surviving} == {2, 5, 6}
    assert stats["dropped"] == 3  # seq 1, 3, 4

    # Reconstruct: a brand-new StateLog over the truncated file.
    log2 = StateLog(path)
    assert log2.current_seq == 6, (
        "tail-read must recover the true watermark (seq 6, itself an "
        "always_keep_kinds entry) — not be fooled into stopping at the low "
        "always_keep_kinds entry (seq 2) or undercounting"
    )
    # The counter must never re-issue a used seq.
    new_seq = asyncio.run(log2.append("inbox_put", target="a_new", payload={}))
    assert new_seq == 7


def test_scan_max_seq_after_truncate_to_single_watermark(tmp_path):
    """Tier 2: edge case — truncate leaves exactly one survivor (the watermark);
    the tail-read must still recover it (not confuse "one line" with "empty")."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 6):
            await log.append("inbox_put", target=f"a{i}", payload={})
        await log.truncate_below(100)  # drop everything but the watermark (seq 5)
        await log.flush()

    asyncio.run(go())

    log2 = StateLog(path)
    assert log2.current_seq == 5


def test_scan_max_seq_matches_full_scan_with_torn_trailing_line(tmp_path):
    """Tier 2: edge case — a torn/incomplete trailing line (crash mid-write) must be
    skipped by the tail-read exactly as the full scan skips it, recovering the last
    WELL-FORMED entry's seq, not raising and not miscounting the torn line."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 4):
            await log.append("inbox_put", target=f"a{i}", payload={})
        await log.flush()

    asyncio.run(go())

    # Inject a torn fragment after the last well-formed entry (no closing brace/newline).
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 99, "kind": "inb')

    log2 = StateLog(path)
    assert log2.current_seq == 3


def test_scan_max_seq_large_wal_parity(tmp_path):
    """Tier 2: a WAL large enough to exceed the tail-read's default backward-read
    window (~500KB, past the 64KB first read) must still recover the exact same
    max_seq as a small WAL would — parity must hold at scale, not only for WALs
    that fit entirely inside the first backward read."""
    path = tmp_path / "wal.jsonl"
    log = StateLog(path)

    async def go():
        for i in range(1, 2001):
            await log.append("inbox_put", target=f"a{i}", payload={"pad": "x" * 200})
        await log.flush()

    asyncio.run(go())

    log2 = StateLog(path)
    assert log2.current_seq == 2000


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
