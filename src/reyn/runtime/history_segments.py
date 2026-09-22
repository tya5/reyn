"""#6240/#6248 — naming and listing primitives for ``history/``'s segment
layout: an ACTIVE segment (always named ``history.jsonl``, the only file the
appender ever writes to) plus zero or more SEALED segments (renamed out of
the appender's way, never written to again).

⭐⭐⭐ Architect design (origin/main issue #6248, comments 5772696821 +
5772712797 — the second one is the CURRENT ruling, see that comment for the
full reasoning this module encodes):

- ``.reyn/agents/<name>/<sid>/history/`` is the new, directory-based home.
  ``history.jsonl`` inside it is always the ACTIVE segment; everything else
  in that directory is a SEALED segment, named ``history-<min_seq>-
  <max_seq>-<s|n>.jsonl`` — the seq range it covers plus whether it
  contains a ``role="summary"`` line (``s``) or not (``n``).
- The OLD, flat ``<name>/<sid>/history.jsonl`` (no ``history/`` directory
  around it) is a PRE-segment-era file. New code never opens it and never
  deletes it (owner ruling: "旧形式の1本fileを見つけても触らない" —
  automatic deletion of a user's own history is not something a product
  does on the user's behalf, only an operator's explicit `reyn storage`
  command does, per that command's own docstring). This module therefore
  has NO function that reads or removes that path — only the segment
  layout under ``history/`` is this module's concern.
- No index/manifest file (architect ruling, comment 5772696821 ②): the two
  facts encoded in a sealed segment's OWN name (seq range, summary
  presence) are exactly what the appender already knows at the instant it
  seals — nothing here is a second source of truth about segment content.

Why the seq range AND the summary flag are both in the name (not just one):
GC (``registry.py``'s ``_gc_one_session_history``) must decide "can this
whole segment be unlinked with 0 bytes read" using only:
  ① the segment's own ``max_seq`` against the WAL floor / hydration margin
     (a filename fact — no read needed)
  ② whether the segment's ``[min_seq, max_seq]`` range is wholly covered by
     an already-recorded compaction fold (computed from the OTHER
     summary-carrying segments only — this is why the flag lets GC skip
     reading every non-summary segment when hunting for fold ranges)
  ③ ``has_summary`` — a segment carrying its own ``role="summary"`` line is
     NEVER unlinked outright (that line is durable fold evidence), matching
     ``rewrite_history_dropping``'s pre-existing "a summary is never
     dropped" invariant, now applied at the whole-segment granularity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The subdirectory name under a session's own workspace dir (mirrors
# ``AgentRegistry._history_dir_for``'s pre-existing 2 construction sites —
# see ``Session.__init__``/``AgentRegistry._history_dir_for`` for the 2
# real callers, spawned vs main session).
HISTORY_DIR_NAME = "history"

# The active segment is ALWAYS this literal name inside ``history/`` — the
# one file the appender ever opens for writing (#6247's own "one writer,
# one path" invariant, now scoped to the active segment specifically).
ACTIVE_SEGMENT_NAME = "history.jsonl"

# #6240: the real 547 MB single-file measurement is what this boundary
# exists to bound. 10 MB mirrors ``AuditEventsConfig.max_bytes`` (config/
# infra.py) — the existing precedent in THIS codebase for "an append-only,
# per-agent JSONL log rotates at N MB" (``.reyn/events``'s own rotation).
# Not wired to a config knob (yet): unlike AuditEventsConfig, no operator
# has asked to tune this, and the architect's own design left the exact
# figure to the implementer ("具体的な命名は実装者の面") — a hard-coded
# value matching an existing precedent is the documented, not-invented,
# default; a config knob can be added the day an operator names a reason.
SEGMENT_MAX_BYTES = 10 * 1024 * 1024

_SEALED_RE = re.compile(r"^history-(\d{12})-(\d{12})-([sn])\.jsonl$")


def history_dir_for(session_workspace_dir: Path) -> Path:
    """The ``history/`` directory for a session whose own workspace dir is
    *session_workspace_dir* (``Session.workspace_dir`` /
    ``AgentRegistry``'s equivalent per-session directory)."""
    return session_workspace_dir / HISTORY_DIR_NAME


def active_segment_path(history_dir: Path) -> Path:
    """The active segment's path inside *history_dir* — always this exact
    name; never renamed while it is the active segment."""
    return history_dir / ACTIVE_SEGMENT_NAME


def sealed_segment_name(min_seq: int, max_seq: int, *, has_summary: bool) -> str:
    """The filename a segment gets renamed TO at seal time (architect design
    ②) — zero-padded so lexicographic sort == numeric sort (segment listing
    below relies on this for the common case, though it always parses and
    sorts numerically rather than trusting the padding alone)."""
    flag = "s" if has_summary else "n"
    return f"history-{min_seq:012d}-{max_seq:012d}-{flag}.jsonl"


@dataclass(frozen=True)
class SealedSegment:
    """One parsed sealed segment — the 3 facts its OWN filename carries,
    plus the path itself. See module docstring for why exactly these 3."""
    path: Path
    min_seq: int
    max_seq: int
    has_summary: bool


def parse_sealed_segment_name(name: str) -> "SealedSegment | None":
    """Parse a bare filename (no directory) into its 3 encoded facts, or
    ``None`` if *name* does not match the sealed-segment pattern (the
    active segment's own name included — callers wanting sealed segments
    ONLY should filter via :func:`list_sealed_segments`, not this
    function alone)."""
    m = _SEALED_RE.match(name)
    if m is None:
        return None
    min_seq, max_seq, flag = int(m.group(1)), int(m.group(2)), m.group(3)
    return SealedSegment(
        path=Path(name), min_seq=min_seq, max_seq=max_seq, has_summary=flag == "s",
    )


def list_sealed_segments(history_dir: Path) -> "list[SealedSegment]":
    """Every sealed segment under *history_dir*, oldest-first (sorted by
    ``min_seq`` — segments never overlap by construction, since the
    appender seals strictly in append order). A missing/non-directory
    *history_dir* returns ``[]`` — nothing sealed yet, not an error (same
    "missing == empty" convention ``history_tail_reader``'s own readers
    use for a missing ``history.jsonl``)."""
    if not history_dir.is_dir():
        return []
    out: "list[SealedSegment]" = []
    for entry in history_dir.iterdir():
        if entry.name == ACTIVE_SEGMENT_NAME:
            continue
        parsed = parse_sealed_segment_name(entry.name)
        if parsed is None:
            continue
        out.append(SealedSegment(
            path=entry, min_seq=parsed.min_seq, max_seq=parsed.max_seq,
            has_summary=parsed.has_summary,
        ))
    out.sort(key=lambda s: s.min_seq)
    return out


def all_segment_paths_oldest_first(history_dir: Path) -> "list[Path]":
    """Every segment path (sealed, oldest-first, THEN the active segment
    last if it exists) — the file-order a forward reader (oldest content
    first) needs. Skips a nonexistent active segment (a session that has
    sealed at least once but not yet written to a fresh active file)."""
    paths = [s.path for s in list_sealed_segments(history_dir)]
    active = active_segment_path(history_dir)
    if active.is_file():
        paths.append(active)
    return paths


def all_segment_paths_newest_first(history_dir: Path) -> "list[Path]":
    """The reverse of :func:`all_segment_paths_oldest_first` — active
    segment first (if present), then sealed segments newest-to-oldest.
    What a backward/tail reader needs (most-recent content first)."""
    return list(reversed(all_segment_paths_oldest_first(history_dir)))
