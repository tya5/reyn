"""#4387 Phase B ①: a bounded, reverse-seeking reader for ``history.jsonl``.

``Session.load_history()`` used to read the WHOLE file forward, building a
``ChatMessage`` for every line ever written — on a long-running session
(owner's real environment: 500MB, #4387) that means every restart re-loads
the entire conversation into memory, which never shrinks even though
compaction keeps what's SENT to the LLM small (compaction shrinks
``build_history``'s output, never ``self.history`` or the file it's read
from).

This module reads backward from EOF instead, stopping once a
``role="summary"`` line has been seen AND at least ``min_lines`` complete
lines have been collected (a reasonable startup scrollback floor even when
the latest compaction happened close to EOF — mirrors ``#3476④``'s own
``_HYDRATE_PAGE_FRAMES=200``). Reading PAST ``min_lines`` to reach a summary
that's further back is always safe — more history in memory is never
incorrect, only more expensive — so ``min_lines`` is a floor, never a cap.

**Without ever finding a summary, this reads the ENTIRE file** (falls all
the way back to BOF) rather than stopping at ``min_lines`` — deliberately
conservative. A ``min_lines``-only stop cannot distinguish "no compaction
has EVER run" (everything in the file is uncompacted, so
``Session._compaction_watermark()``-bounded consumers need ALL of it, not
just the last ``min_lines``) from "there IS a summary, just further back
than ``min_lines``" — both look identical short of BOF, so BOF is the only
safe fallback. This is not worse than the pre-#4387 always-full-read for
that rare case, and a session that HAS compacted at least once (the
realistic shape once a session is old/large enough to matter, since
compaction keeps re-firing as the token budget refills) gets the bounded
read.

Older entries this reader doesn't return are NOT lost: ``history.jsonl`` is
append-only, so anything left unread here is still on disk, reachable later
via the extend-on-demand path (#4387 Phase B ②, not yet implemented).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def _iter_raw_lines_reverse(path: Path, *, chunk_size: int) -> "Iterator[str]":
    """Yield *path*'s lines one at a time, newest-first, reading backward
    from EOF in growing-safe chunks. Shared by :func:`read_history_tail`
    and :func:`read_history_before` (#4387 Phase B ②) so the carry/split
    handling — the part actually worth getting wrong once, not twice —
    lives in one place. Caller decides when to stop (this generator itself
    never stops early; it exhausts to BOF unless the caller breaks)."""
    size = path.stat().st_size
    if size == 0:
        return
    pos = size
    # Bytes read from the START of the previous (further-back) chunk that
    # didn't yet complete a line when that chunk was split — prefixed onto
    # the NEXT (even-further-back) read so a line straddling a chunk
    # boundary is never mis-split. Grows without bound only in the
    # pathological case of one line longer than chunk_size, same tolerance
    # ``budget.py``'s ``tail_boundary`` gives that case.
    carry = b""
    with path.open("rb") as f:
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + carry
            parts = buf.split(b"\n")
            if pos > 0:
                carry = parts[0]
                complete = parts[1:]
            else:
                carry = b""
                complete = parts
            for raw in reversed(complete):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    yield line


def read_last_line(path: Path, *, chunk_size: int = 8192) -> "str | None":
    """The last complete line of *path*, or ``None`` if missing/empty.

    Cheap (bounded by one line's length, not file size) — mirrors
    ``budget.py``'s ``BudgetTracker.tail_boundary``. Used to decide, BEFORE
    committing to a bounded tail read, whether the file's last entry has a
    real assigned ``seq`` (post-#3704: every append does) — if it does not,
    ``load_history`` falls back to a full forward read rather than trust a
    partial tail slice for ``_next_seq`` derivation (see that module's own
    reasoning)."""
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size == 0:
        return None
    read_size = min(chunk_size, size)
    with path.open("rb") as f:
        f.seek(size - read_size)
        buf = f.read(read_size)
    search_end = len(buf) - 1 if buf.endswith(b"\n") else len(buf)
    idx = buf.rfind(b"\n", 0, search_end)
    if idx == -1:
        if read_size < size:
            return read_last_line(path, chunk_size=chunk_size * 4)
        line_bytes = buf
    else:
        line_bytes = buf[idx + 1:]
    line = line_bytes.decode("utf-8", errors="replace").strip()
    return line or None


def read_history_tail(
    path: Path, *, min_lines: int = 200, chunk_size: int = 65536,
) -> list[str]:
    """Read *path* backward from EOF, returning raw JSON-line strings in
    FILE order (oldest line in the returned slice first). Empty list if the
    file is missing or empty. See module docstring for the stop condition.
    """
    if not path.is_file():
        return []

    collected: list[str] = []
    seen_summary = False
    # Stop condition is (seen_summary AND collected >= min_lines) — NEVER
    # "collected >= min_lines" alone. Without a summary, we cannot tell
    # "no compaction has EVER run" (everything is uncompacted, so a
    # short-circuit here would violate the watermark-completeness invariant
    # every bounded consumer depends on) apart from "the summary is just
    # further back than min_lines" (test_read_history_tail_reads_past_the_
    # floor_to_include_the_latest_summary) — both look identical until BOF
    # is actually reached, so BOF is the only safe fallback stop.
    for line in _iter_raw_lines_reverse(path, chunk_size=chunk_size):
        collected.append(line)
        if not seen_summary:
            try:
                if json.loads(line).get("role") == "summary":
                    seen_summary = True
            except (json.JSONDecodeError, AttributeError):
                pass
        if len(collected) >= min_lines and seen_summary:
            break

    collected.reverse()
    return collected


def read_history_after(
    path: Path, *, after_seq: int, chunk_size: int = 65536,
) -> list[str]:
    """#4472: read *path* backward from EOF, collecting every line whose
    ``seq > after_seq`` (a real, nonzero seq — a ``seq == 0`` legacy line
    is ALWAYS collected too, matching #4387 Phase A's own precedent for
    pre-#3704 entries with no assigned coordinate), stopping the moment a
    line with a real ``seq <= after_seq`` is seen. Safe to stop early
    because ``seq`` is non-decreasing by file position (every append since
    #3704 assigns a strictly-increasing coordinate) — once a real seq at or
    below the threshold is reached, everything further back is too.
    Returns raw JSON-line strings in FILE order (oldest first). Empty list
    if the file is missing/empty, or if EOF itself is at/below
    ``after_seq``.

    #4472's own reason for existing: ``CompactionController`` used to build
    its candidates from ``Session.history`` (an in-memory, byte-cap-
    evictable cache — #4387/#4468), so a resource-role eviction could make
    compaction blind to content it had never actually summarized, silently
    letting ``covers_through_seq`` claim coverage of a gap (#4470). Reading
    directly from the DURABLE store here removes residency from the
    question entirely — compaction always sees the true uncompacted range,
    regardless of what's currently resident in memory.

    No ``min_lines`` floor (unlike :func:`read_history_tail`/
    :func:`read_history_before`): compaction needs the COMPLETE range above
    ``after_seq``, not a bounded page — silently truncating it would
    reintroduce exactly the "claimed more coverage than it examined" defect
    this function exists to prevent.
    """
    if not path.is_file():
        return []

    collected: list[str] = []
    for line in _iter_raw_lines_reverse(path, chunk_size=chunk_size):
        try:
            seq = int(json.loads(line).get("seq", 0) or 0)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            seq = 0
        if seq != 0 and seq <= after_seq:
            break
        collected.append(line)

    collected.reverse()
    return collected


def read_history_before(
    path: Path, *, before_seq: int, min_lines: int = 200, chunk_size: int = 65536,
) -> list[str]:
    """#4387 Phase B ②: read *path* backward from EOF, SKIPPING every line
    whose ``seq >= before_seq`` (already held in memory by whoever is
    calling this — see ``Session._load_older_entries``), then collecting
    up to ``min_lines`` further lines older than that. Returns raw JSON-line
    strings in FILE order (oldest first). Empty list if the file is
    missing/empty, or if BOF is reached before any qualifying line is seen.

    Unlike :func:`read_history_tail`, this has NO safety floor analogous to
    "must include a summary" — a caller extending an ALREADY-loaded prefix
    backward is, by construction, not making a fresh completeness claim
    about compaction watermarks (that invariant was already satisfied by
    whatever got the caller its current ``self.history`` in the first
    place); it is only answering "give me up to N more lines older than
    what I already have," which ``min_lines`` alone answers correctly.
    """
    if not path.is_file():
        return []

    collected: list[str] = []
    for line in _iter_raw_lines_reverse(path, chunk_size=chunk_size):
        try:
            seq = int(json.loads(line).get("seq", 0) or 0)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            seq = 0
        if seq >= before_seq:
            continue
        collected.append(line)
        if len(collected) >= min_lines:
            break

    collected.reverse()
    return collected
