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

#4476 Phase 1 adds a small, separate section at the end of this module:
policy-independent measurement (bytes/line counts across every
``history.jsonl``) that reads to no truncation path — see that section's
own header comment for why it lives here rather than a new module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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


# #4477: named so `router_history_buffer._check_compaction_batch_within_
# budget` (the 4th resource/budget-role comparison instance, #4381 PR-1's
# own class) can compare against the SAME value this function actually
# defaults to — a second, independently-typed `8 * 1024 * 1024` literal
# would silently drift from this one the day either changes.
COMPACTION_BATCH_MAX_BYTES: int = 8 * 1024 * 1024


def read_history_after(
    path: Path, *, after_seq: int, max_bytes: int = COMPACTION_BATCH_MAX_BYTES,
) -> "tuple[list[str], bool]":
    """#4472 (batched, per architect's + lead-coder's independent review of
    the first draft): read *path* FORWARD from BOF, skipping every line
    with a real, nonzero ``seq <= after_seq`` (streamed — never
    materialized, so the skipped prefix costs nothing but a linear scan,
    not memory), then collecting up to ``max_bytes`` worth of the
    remaining lines (a ``seq == 0`` legacy line is always collected,
    matching #4387 Phase A's own precedent for pre-#3704 entries with no
    assigned coordinate). Returns ``(lines, truncated)`` in FILE order
    (oldest first) — ``truncated=True`` means more qualifying content
    exists past what was returned; the caller must treat the highest seq
    actually returned as the new coverage boundary, NOT assume the whole
    ``(after_seq, EOF]`` range was examined.

    **Why a byte cap here does NOT reintroduce #4470** (the correction
    lead-coder's review made explicit, after the first, unbatched draft's
    docstring incorrectly conflated the two): #4470's actual defect was
    SKIPPING content — a range between ``prev_cover`` and the highest
    examined seq that was silently never looked at, yet marked covered.
    Reading CONTIGUOUSLY starting immediately after ``after_seq`` and
    treating the LAST LINE ACTUALLY READ as the new boundary (never
    "the highest seq that theoretically exists") means nothing in the
    returned range is ever unexamined-but-claimed-covered — a batch just
    makes PARTIAL, HONEST progress each call, needing multiple compaction
    passes to work through a large backlog, exactly the way #4470's own
    fix (contiguous coverage, never a gap) already required. What #4470
    forbids is skipping ahead past unseen content, not reading less of it
    per call.

    #4472's own reason for existing: ``CompactionController`` used to build
    its candidates from ``Session.history`` (an in-memory, byte-cap-
    evictable cache — #4387/#4468), so a resource-role eviction could make
    compaction blind to content it had never actually summarized, silently
    letting ``covers_through_seq`` claim coverage of a gap (#4470). Reading
    directly from the DURABLE store here removes residency from the
    question entirely — compaction always sees the true uncompacted range,
    regardless of what's currently resident in memory, while THIS batch
    cap keeps a single compaction pass from materializing an unbounded
    amount when the unsummarized backlog is large (a stalled/bursty
    session) — the exact memory-unboundedness architect's review measured
    in the first, unbatched draft.

    ``max_bytes`` default (8 MiB) is a starting point, not load-bearing —
    #4387's own history_resident cap defaults to 256 MiB for the RESIDENT
    array; this transient, single-pass batch is intentionally much smaller
    since it exists only for the duration of one compaction call, never
    held long-term.

    **Measured (lead-coder's #4472 review, requirement ③ — verified live,
    not asserted)**: ``CompactionController._select_candidates``'s
    ``trim_head``/``trim_tail`` operate purely on whatever ``turns`` list
    they're handed — no dependency on the full/unbatched conversation
    size, confirmed by driving a real compaction pass with an
    artificially tiny batch cap (500 bytes) vs the real 8 MiB default
    against a 2000-turn (~800 KB raw) backlog: the tiny cap produced 0
    candidates (the whole batch was consumed by head+tail trimming alone,
    leaving no middle) while the real default correctly summarized all
    1999 eligible turns in one pass (well under 8 MiB). So a batch cap
    that is NOT comfortably larger than ``head_budget + tail_budget``'s
    own combined token footprint can make a compaction pass produce zero
    candidates — no progress, though also no incorrect coverage claim
    (this function's own contiguous-batch discipline holds regardless).

    ⚠️ **Architect's follow-up correction (same PR, not a merge blocker):**
    the 2000-turn measurement above only verified the BACKLOG-SIZE axis —
    it does NOT verify the axis that actually decides whether the "zero
    candidates" condition triggers, which is independent of backlog size
    entirely: whether ``max_bytes`` (a RESOURCE-role byte cap) is smaller
    than ``head_budget + tail_budget`` (a BUDGET-role token figure,
    MODEL-context-window-derived, #4431's role split). A small backlog
    proves nothing about a large-context-window model whose head+tail
    budgets alone could exceed 8 MiB. Worse than "no progress": zero
    candidates means no summary, means ``covers_through_seq`` never
    advances, means the NEXT pass reads the identical window and produces
    the identical zero — a genuine STALL, the exact class #4471/#4472
    exist to close, reachable through this new resource/budget
    combination. Neither architect nor this author has computed
    ``head_budget + tail_budget``'s own worst case, so whether this
    actually triggers at the 8 MiB default is UNVERIFIED, not ruled out —
    tracked as issue #4477 (a warn-once check at the same conversion point
    ``INLINE_CAP_BYTES_PER_TOKEN`` already established for this class of
    resource/budget comparison, #4381 PR-1 precedent) rather than solved
    here. #4477's own first task is measuring ``head_budget + tail_budget``'s
    worst case, not implementing the warn — reachability is confirmed
    before a mechanism is built.
    """
    if not path.is_file():
        return [], False

    collected: list[str] = []
    total_bytes = 0
    truncated = False
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                seq = int(json.loads(line).get("seq", 0) or 0)
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                seq = 0
            if seq != 0 and seq <= after_seq:
                continue
            line_bytes = len(line.encode("utf-8"))
            if collected and total_bytes + line_bytes > max_bytes:
                truncated = True
                break
            collected.append(line)
            total_bytes += line_bytes

    return collected, truncated


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


# ── #4476 Phase 1: read-only measurement, no truncation ─────────────────
#
# retention.py:84-87's own docstring states ``history.jsonl`` is "append-only
# and never floor-truncated" — this file has NO deletion path today, by
# design (branch visibility / compaction's own watermark / TUI scrollback
# all depend on old lines still being readable, per #4476's own issue body).
# The owner-measured 500MB figure (#4387) is a single point — one
# environment, one moment — too thin to set a retention policy from. This
# section exists ONLY to widen that single point into an actual measured
# population, same order as #4478/#4485: land the measurement, let evidence
# accumulate, THEN an owner-set policy (never invented here) decides
# anything. See this module's own docstring for why truncation isn't safe
# to add casually, and :func:`read_history_after`'s docstring for the
# ``covers_through_seq`` floor a Phase 2 truncation would eventually have to
# respect.


@dataclass(frozen=True)
class HistoryStorageStats:
    """#4476 Phase 1: policy-independent snapshot of every discovered
    ``history.jsonl``'s on-disk footprint under a project's ``.reyn/agents/``
    tree. Measurement only — no field here implies or drives any deletion."""
    file_count: int
    total_bytes: int
    total_lines: int


def history_file_stats(path: Path) -> "tuple[int, int]":
    """``(bytes, lines)`` for one ``history.jsonl``. ``bytes`` is the exact
    on-disk file size (``stat().st_size``); ``lines`` counts non-empty
    JSONL lines (= turns), the same "blank line is not an entry" rule
    :func:`read_history_after` already uses — so this count agrees with
    what a durable-store reader actually sees, not a raw ``wc -l``.
    Missing file → ``(0, 0)``, matching "nothing written yet" rather than
    an error."""
    if not path.is_file():
        return 0, 0
    total_bytes = path.stat().st_size
    lines = 0
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            if raw.strip():
                lines += 1
    return total_bytes, lines


def aggregate_history_stats(project_root: Path) -> HistoryStorageStats:
    """#4476 Phase 1: sum :func:`history_file_stats` over every
    ``history.jsonl`` found anywhere under ``<project_root>/.reyn/agents/``
    (``**/history.jsonl`` — covers both a top-level agent's own file and any
    nested spawned-session workspace, without hardcoding the exact nesting
    depth, which is an internal detail of ``registry.py`` this module has no
    reason to duplicate). A project with no ``.reyn/agents/`` yet returns
    all-zero, not an error."""
    agents_dir = project_root / ".reyn" / "agents"
    if not agents_dir.is_dir():
        return HistoryStorageStats(file_count=0, total_bytes=0, total_lines=0)
    file_count = 0
    total_bytes = 0
    total_lines = 0
    for hist_path in sorted(agents_dir.glob("**/history.jsonl")):
        b, lines = history_file_stats(hist_path)
        file_count += 1
        total_bytes += b
        total_lines += lines
    return HistoryStorageStats(
        file_count=file_count, total_bytes=total_bytes, total_lines=total_lines,
    )
