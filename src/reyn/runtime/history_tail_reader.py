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
    size = path.stat().st_size
    if size == 0:
        return []

    collected: list[str] = []
    seen_summary = False
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
                # parts[0] may be a partial line — carry it into the next
                # (further-back) read rather than treating it as complete.
                carry = parts[0]
                complete = parts[1:]
            else:
                carry = b""
                complete = parts

            # Checked PER LINE, not just per chunk: a chunk can (and for a
            # small file, on the very first read, always does) contain far
            # more than min_lines — stopping only at chunk boundaries would
            # silently ignore the floor whenever one read covers the whole
            # remaining file.
            #
            # Stop condition is (seen_summary AND collected >= min_lines) —
            # NEVER "collected >= min_lines" alone. Without a summary, we
            # cannot tell "no compaction has EVER run" (everything is
            # uncompacted, so a short-circuit here would violate the
            # watermark-completeness invariant every bounded consumer
            # depends on) apart from "the summary is just further back than
            # min_lines" (test_read_history_tail_reads_past_the_floor_to_
            # include_the_latest_summary) — both look identical until BOF is
            # actually reached, so BOF is the only safe fallback stop.
            done = False
            for raw in reversed(complete):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                collected.append(line)
                if not seen_summary:
                    try:
                        if json.loads(line).get("role") == "summary":
                            seen_summary = True
                    except (json.JSONDecodeError, AttributeError):
                        pass
                if len(collected) >= min_lines and seen_summary:
                    done = True
                    break
            if done:
                break

    collected.reverse()
    return collected
