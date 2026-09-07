"""#5896 stage ③ (owner-hit P0, 2026-09-07): the offline, operator-run
migration for tool-result BODIES already inline in a ``history.jsonl`` line
BEFORE stage ① (#5901) started writing every new tool result as a ref at
return time. Wired as ``reyn storage migrate-bodies`` — see
``interfaces/cli/commands/storage.py``.

WHY THIS EXISTS
    Owner-hit incident: a single 369 MB inline row (plus a 157.8 MB
    sibling, 527 MB total between the two) drove ``reyn:web``'s own
    startup ``phys_footprint`` peak to 8226 MB inside 3 minutes, during
    which the loop held CPU at 70.9% on one thread and the owner's own
    ``--connect`` went unanswered. Architect's own audit of every existing
    mechanism (issue #5896's comment thread) found NONE of them reach an
    already-written row: #5944's grep cap and stage ①'s return-time write
    only ever apply to a FUTURE tool result; ``/compact``'s reactive spill
    appends a supersede record but never rewrites the ORIGINAL row (its
    inline body stays exactly where it was); #5759's history GC requires
    the row to already be folded, and the owner's own history has only 7
    summaries against the largest (newest) rows, seq 5851-5998, none of
    which are folded yet. This command is the only tool that reaches
    already-on-disk history.

WHAT IT DOES
    For each ``role="tool"`` row whose ``content`` is still a non-empty,
    un-ref'd string of at least ``min_bytes``: either (a) reuse an
    EXISTING history-content file if a prior reactive spill already wrote
    this row's exact content (a ``spill_record`` row in the SAME file
    names it by content hash — reusing it costs ZERO new bytes), or (b)
    write the body out fresh via the injected *save_fn* (production:
    ``MediaStore.save_tool_result(..., spilled=False)``, the SAME
    un-spilled write shape stage ① already uses at return time) when no
    such record exists. Either way the row is rewritten to the exact
    on-disk shape ``history_record()`` (``chat_message.py``) already
    produces for a stage-① un-spilled row: ``content=""``,
    ``meta[content_ref]=<ref>`` — read back through the SAME resolver
    (``Session._parse_history_line``) every other un-spilled row already
    goes through, not a new code path.

    Per architect's corrected accounting (the two largest rows, 527 MB
    total, are NOT already spilled — ``history-content/``'s largest file
    on disk is 20 MB, well under either): the common case here IS a fresh
    527 MB write, not a free ref-copy. Disk headroom for that write
    (roughly the sum of every migrated row's own size) is the operator's
    own responsibility to have available before running this — the same
    posture ``reyn storage stats`` already exists to inform.

WRITE-AHEAD, PER ROW (stage ①'s same ordering, never reversed)
    The body file is written and closed BEFORE this row's own line is
    written into the rewritten output — so a crash between the two always
    leaves ``history.jsonl`` UNCHANGED (the original row, inline body,
    still there) rather than a row whose ref names a file that was never
    finished. A body file written this way but never referenced by a
    completed migration is an orphan, not a corruption (harmless, exactly
    stage ①'s own established write-ahead property) — see "WHAT AN
    INTERRUPTED RUN LEAVES BEHIND" below.

FILE-LEVEL ATOMICITY AND ``.bak``
    Mirrors :func:`reyn.runtime.history_tail_reader.rewrite_history_dropping`'s
    own mechanics (stream-read, write survivors — here, TRANSFORMED rows —
    to a ``.tmp``, ``fsync``) but adds a REAL backup rename this repo's
    existing rewrite path does not need (that one only ever DROPS lines
    that reload from disk anyway; this one changes what a row durably
    means, so the pre-migration file must survive as a real copy, not
    just implicitly via git or a snapshot). Sequence: write ``.tmp``,
    fsync, rename the ORIGINAL to ``.bak`` (atomic on POSIX — the original
    is untouched until this single step), then rename ``.tmp`` into the
    original's place (also atomic). A ``.bak`` from an earlier run is
    NEVER silently overwritten — see below.

WHAT AN INTERRUPTED RUN LEAVES BEHIND (owner-hit correction: this must be
designed, not assumed)
    - Crash during the per-row scan/write phase: ``history.jsonl`` is
      UNCHANGED (still the pre-migration file — the rewrite only ever
      touches a separate ``.tmp``). Any body files already written by
      *save_fn* for rows not yet reflected in ``.tmp`` are orphans:
      wasted disk, never referenced by anything, never read by anything
      (nothing points at them yet). Re-running the command from scratch
      is safe — it will re-scan the SAME still-inline rows and write
      FRESH files again (a plain content-addressed dedup is out of scope
      here; see :func:`reyn.data.workspace.media_store.save_tool_result`
      for why this module does not attempt one on top of it).
    - Crash between the two renames (extremely narrow — a plain OS
      ``rename`` on the SAME filesystem is not interruptible mid-call by
      normal process death): at worst the operator sees a ``.bak``
      without a rewritten ``history.jsonl`` yet, or vice versa; neither
      state loses data — the ``.bak`` is a complete, valid copy of the
      pre-migration file either way.
    - A pre-existing ``.bak`` (a PRIOR run's backup, not yet cleaned up
      by the operator): this run REFUSES to proceed rather than guess
      whether it is safe to overwrite — see :func:`migrate_inline_history_bodies`'s
      own ``FileExistsError``.

WHY A MIN-BYTES FLOOR, NOT "MIGRATE EVERYTHING"
    An ordinary, already-small tool-result row (bytes below the floor)
    gains nothing from a ref indirection and pays a stat/open round-trip
    on every future read for no measured benefit — CLAUDE.md's "no
    unjustified constant without a rationale or a knob": both are given
    here. ``DEFAULT_MIN_BYTES`` (1 MiB) is NOT derived from a host-RAM
    ratio or any per-row cap this repo already enforces elsewhere (there
    is none for a return-time write predating stage ①) — it is a
    round, conservative floor chosen so an ordinary tool result (a file
    read, a small command's stdout) is never migrated while a genuinely
    oversized one (the owner's own 369 MB / 157.8 MB rows, four further
    orders of magnitude above this floor) always is. ``--min-bytes`` on
    the CLI overrides it for an operator who wants a different cut.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

from reyn.runtime.chat_message import (
    CONTENT_REF_META_KEY,
    SPILL_TARGET_CONTENT_HASH_META_KEY,
    SPILLED_META_KEY,
)

#: See module docstring's "WHY A MIN-BYTES FLOOR" for the rationale.
DEFAULT_MIN_BYTES = 1024 * 1024  # 1 MiB

#: Injected write primitive: raw body text in, the project-relative ref
#: path out. Production: a closure over a real
#: ``MediaStore.save_tool_result(content, spilled=False, ...)`` call,
#: returning its own ``result["path"]`` — see ``interfaces/cli/commands/
#: storage.py``'s own wiring. Injected (not imported here) for the same
#: reason ``migrate_history_content_manifest`` (``media_store.py``)
#: injects its own reader: this module reads ``ChatMessage``'s meta-key
#: vocabulary (a runtime-layer concept) and must not import
#: ``reyn.data.workspace``'s write machinery directly, keeping the
#: layering the same direction that module's own docstring establishes.
SaveBodyFn = Callable[[str], str]


def _content_hash(content: str) -> str:
    """The EXACT hash ``RouterHistoryBuffer.spill_turn_content`` (#5612)
    already computes and stores on every reactive-spill supersede
    record — reusing the identical algorithm is what lets this module
    recognize "this row's body was already spilled durably" and reuse
    that existing file instead of writing a second copy."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _spill_supersede_refs(entries: "list[dict]") -> "dict[str, str]":
    """Scan already-parsed ``history.jsonl`` entries for ``spill_record``
    rows (#5612's own durable supersede format) and return a
    ``content_hash -> ref`` map — the SAME fact
    ``RouterHistoryBuffer._spill_supersede_map`` derives from live
    resident history, rebuilt here from the raw on-disk lines since this
    module runs with no live session (the whole point of an operator
    command run while the session is stopped)."""
    refs: "dict[str, str]" = {}
    for entry in entries:
        if entry.get("role") != "spill_record":
            continue
        meta = entry.get("meta")
        if not isinstance(meta, dict):
            continue
        target_hash = meta.get(SPILL_TARGET_CONTENT_HASH_META_KEY)
        ref = meta.get(CONTENT_REF_META_KEY)
        if isinstance(target_hash, str) and isinstance(ref, str):
            # #5628's own precedent (first-write-wins on a re-scan): an
            # EARLIER spill_record for the same hash names the file that
            # has existed longest; never overwrite with a later one.
            refs.setdefault(target_hash, ref)
    return refs


def _is_migration_candidate(entry: dict, *, min_bytes: int) -> bool:
    if entry.get("role") != "tool":
        return False
    content = entry.get("content")
    if not isinstance(content, str) or content == "":
        return False
    meta = entry.get("meta")
    if isinstance(meta, dict) and meta.get(CONTENT_REF_META_KEY):
        return False  # already ref'd (stage ① write, or an earlier migration run)
    return len(content.encode("utf-8")) >= min_bytes


def migrate_inline_history_bodies(
    path: Path,
    *,
    save_fn: SaveBodyFn,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> "dict[str, int]":
    """The whole migration for ONE ``history.jsonl`` file — see module
    docstring for the write-ahead/atomicity/interrupted-run contract.

    Returns ``{"migrated": <rows rewritten>, "reused_ref": <of those, how
    many cost zero new bytes via an existing spill record>,
    "bytes_written": <sum of body bytes actually written fresh>}``. A
    missing file, or one with nothing over ``min_bytes``, returns all
    zeros and never touches the filesystem beyond the read.

    Raises :class:`FileExistsError` if a ``.bak`` from an earlier run is
    still present — refuses to guess whether overwriting it is safe (see
    module docstring's "WHAT AN INTERRUPTED RUN LEAVES BEHIND")."""
    if not path.is_file():
        return {"migrated": 0, "reused_ref": 0, "bytes_written": 0}

    bak_path = path.with_name(path.name + ".bak")
    if bak_path.exists():
        raise FileExistsError(
            f"{bak_path} already exists — refusing to run (it may be an "
            f"earlier interrupted migration's own backup). Move or remove "
            f"it first if you have confirmed it is safe to discard."
        )

    raw_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    parsed: "list[dict | None]" = []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            parsed.append(None)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            parsed.append(None)
            continue
        parsed.append(entry if isinstance(entry, dict) else None)

    existing_refs = _spill_supersede_refs([e for e in parsed if e is not None])

    migrated = 0
    reused_ref = 0
    bytes_written = 0
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as dst:
        for raw, entry in zip(raw_lines, parsed):
            if entry is None or not _is_migration_candidate(entry, min_bytes=min_bytes):
                dst.write(raw)
                continue
            content = entry["content"]
            content_hash = _content_hash(content)
            ref = existing_refs.get(content_hash)
            if ref is not None:
                reused_ref += 1
            else:
                # Write-ahead: the body file is fully written (save_fn's
                # own contract, matching MediaStore.save_tool_result's
                # write-then-return) BEFORE this row's rewritten line is
                # ever written to tmp_path below.
                ref = save_fn(content)
                bytes_written += len(content.encode("utf-8"))
            new_meta = dict(entry.get("meta") or {})
            new_meta[CONTENT_REF_META_KEY] = ref
            new_meta.pop(SPILLED_META_KEY, None)  # the un-spilled shape, stage ①'s own precedent
            new_entry = dict(entry)
            new_entry["content"] = ""
            new_entry["meta"] = new_meta
            dst.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
            migrated += 1
        dst.flush()
        os.fsync(dst.fileno())

    if migrated == 0:
        tmp_path.unlink()
        return {"migrated": 0, "reused_ref": 0, "bytes_written": 0}

    path.rename(bak_path)
    tmp_path.rename(path)
    return {"migrated": migrated, "reused_ref": reused_ref, "bytes_written": bytes_written}
