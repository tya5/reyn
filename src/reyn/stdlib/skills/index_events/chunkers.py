"""chunkers.py — pure-function API for index_events stdlib skill (FP-0009 A).

Public pure functions (Tier 2 testable — no artifact dict, no global state):
  collect_run_chunks  — walk events_root, group by run boundary, return chunks
  advance_cursor      — atomic write of new max ts to cursor file
  read_cursor         — read cursor file; return None if missing

Postprocessor entry points (called by the skill harness with artifact dict):
  run_collect_chunks  — artifact wrapper around collect_run_chunks
  run_advance_cursor  — artifact wrapper around advance_cursor

P7 note: this module is skill-local and may freely reference event-domain
concepts (run boundaries, skill names, tool_executed, etc.). OS code does NOT
import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ── Constants ─────────────────────────────────────────────────────────────────

_CHUNKS_JSONL_PATH = "artifacts/event_chunks.jsonl"
_ERROR_EXCERPT_MAX = 200
_EPOCH_ISO = "1970-01-01T00:00:00Z"


# ── Public pure functions ──────────────────────────────────────────────────────


def collect_run_chunks(events_root: str, since: str | None) -> list[dict]:
    """Walk events_root/**/*.jsonl, group by run boundary, produce run-chunk dicts.

    A "complete run" is delimited by run_skill_started → run_skill_completed
    (or run_skill_failed/workflow_finished/workflow_failed). Incomplete runs
    (in-flight; no completion event) are skipped — they will be picked up on
    the next index pass.

    Each chunk dict has the shape expected by the embed op:
        {
            "id": "<skill>__<run_id>",
            "text": "<formatted run summary, multiline>",
            "metadata": {
                "skill": str,
                "skill_version_hash": str,   # "unknown" if absent (FP-0006 A compat)
                "status": "success" | "failed" | "aborted",
                "started_at": str,
                "ended_at": str,
                "duration_seconds": float | None,
                "caller": str,               # from run_id prefix or "direct"
                "errors": list[str],         # truncated to 3 entries
                # ChunkMetadata-compatible fields:
                "source_path": str,
                "source_type": str,
                "content_hash": str,
                "embedding_model": str,
                "chunk_index": int,
                "size_tokens": int,
                "parent_context": None,
                "extra": dict,
            }
        }

    Args:
        events_root: Path to root containing **/*.jsonl event files.
        since: ISO-8601 lower bound (inclusive by completed_at). None = all runs.

    Returns:
        List of chunk dicts for complete runs whose completed_at >= since.
        Skips in-flight runs (no completion event).
    """
    since_dt: datetime | None = None
    if since:
        since_dt = _parse_iso_safe(since)

    event_files = _discover_event_files(events_root)
    chunks: list[dict] = []
    chunk_index = 0

    for run_id, events, source_file in _stream_runs(event_files):
        result = _build_chunk(run_id, events, source_file, since_dt, skill_filter=None)
        if result is None or result.get("_filtered"):
            continue  # skipped (in-flight) or filtered
        result["metadata"]["chunk_index"] = chunk_index
        chunks.append(result)
        chunk_index += 1

    return chunks


def advance_cursor(cursor_path: str, new_ts: str) -> None:
    """Atomic write of new max ts to cursor file.

    Creates parent directories as needed. Uses tempfile + os.rename for
    atomic update (crash-safe). Raises OSError on persistent write failure.
    """
    path = Path(cursor_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".events_cursor_tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_ts)
        os.rename(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cursor(cursor_path: str) -> str | None:
    """Read cursor file; return None if missing or empty."""
    path = Path(cursor_path)
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value if value else None
    except OSError:
        return None


# ── Postprocessor entry points (artifact-dict wrappers) ──────────────────────


def run_collect_chunks(artifact: dict) -> dict:
    """Postprocessor python step: chunk events into run-unit JSONL.

    Receives the full postprocessor artifact (= LLM scan_plan merged with
    skill input data). Extracts `since` and `skill_filter` from the LLM
    artifact, then re-globs event files deterministically from the workspace
    `.reyn/events/` directory.

    IMPORTANT: This function intentionally does NOT read `event_files` from
    the artifact. The scan preprocessor no longer exposes the full file list
    to the LLM (BUG-1 fix: the list exceeded ARTIFACT_REF_THRESHOLD, causing
    the OS to compress it to an artifact_ref the LLM could not dereference,
    leading to hallucinated file paths and chunk_count=0). File discovery is
    purely deterministic and belongs in the postprocessor, not in LLM context.

    Writes chunks to artifacts/event_chunks.jsonl for the embed op.
    Returns summary dict placed at data.chunk_stats:
        {
            "chunk_count":   int,
            "skipped_runs":  int,
            "filtered_runs": int,
        }
    """
    data = artifact.get("data") or {}
    since_str: str | None = str(data.get("since") or "") or None
    skill_filter_raw = data.get("skill_filter")
    skill_filter: list[str] | None = list(skill_filter_raw) if skill_filter_raw else None

    since_dt: datetime | None = None
    if since_str and since_str != _EPOCH_ISO:
        since_dt = _parse_iso_safe(since_str)

    # Re-glob event files deterministically — do NOT use data.event_files from
    # the LLM artifact (it is no longer provided; BUG-1 fix).
    events_root = str(Path(".reyn") / "events")
    file_paths = _discover_event_files(events_root)

    output_path = Path(_CHUNKS_JSONL_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_count = 0
    skipped_runs = 0
    filtered_runs = 0
    chunk_index = 0

    with open(output_path, "w", encoding="utf-8") as out_f:
        for run_id, events, source_file in _stream_runs(file_paths):
            result = _build_chunk(run_id, events, source_file, since_dt, skill_filter)
            if result is None:
                skipped_runs += 1
                continue
            if result.get("_filtered"):
                filtered_runs += 1
                continue
            result["metadata"]["chunk_index"] = chunk_index
            record = {
                "text": result["text"],
                "metadata": result["metadata"],
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            chunk_count += 1
            chunk_index += 1

    return {
        "chunk_count": chunk_count,
        "skipped_runs": skipped_runs,
        "filtered_runs": filtered_runs,
    }


def run_advance_cursor(artifact: dict) -> dict:
    """Postprocessor python step: advance .reyn/index/events_cursor.

    Reads the max completed_at from written event_chunks.jsonl, then calls
    advance_cursor() to write the new value atomically.

    Returns summary placed at data.cursor_result:
        {
            "indexed_runs":    int,
            "skipped_runs":    int,
            "filtered_runs":   int,
            "new_cursor":      str,
            "sources_updated": list[str],
        }
    """
    data = artifact.get("data") or {}
    chunk_stats = data.get("chunk_stats") or {}

    indexed_runs = int(chunk_stats.get("chunk_count") or 0)
    skipped_runs = int(chunk_stats.get("skipped_runs") or 0)
    filtered_runs = int(chunk_stats.get("filtered_runs") or 0)

    cursor_path = str(Path(".reyn") / "index" / "events_cursor")
    new_cursor = _find_max_cursor_from_chunks()
    if not new_cursor:
        existing = read_cursor(cursor_path)
        new_cursor = existing if existing else _EPOCH_ISO

    advance_cursor(cursor_path, new_cursor)

    return {
        "indexed_runs": indexed_runs,
        "skipped_runs": skipped_runs,
        "filtered_runs": filtered_runs,
        "new_cursor": new_cursor,
        "sources_updated": ["events"],
    }


# ── Core internal logic ───────────────────────────────────────────────────────


def _stream_runs(
    event_files: list[Path],
) -> Iterator[tuple[str, list[dict], str]]:
    """Yield (run_id, events, source_file) tuples, grouping events by run_id.

    Reads all files into memory (grouped by run_id), then yields each group.
    run_id is extracted from event.data.run_id when present; falls back to
    a (skill, timestamp) composite key for legacy events without run_id.
    """
    runs: dict[str, list[dict]] = defaultdict(list)
    run_files: dict[str, str] = {}

    for file_path in event_files:
        if not file_path.exists():
            continue
        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    run_id = _extract_run_id(event)
                    runs[run_id].append(event)
                    if run_id not in run_files:
                        run_files[run_id] = str(file_path)
        except OSError:
            continue

    for run_id, events in runs.items():
        yield run_id, events, run_files.get(run_id, "")


def _build_chunk(
    run_id: str,
    events: list[dict],
    source_file: str,
    since_dt: datetime | None,
    skill_filter: list[str] | None,
) -> dict | None:
    """Build a chunk dict from a run's events, or return None to skip.

    Returns:
        chunk dict (with _filtered=True for filtered runs) |
        None for incomplete (in-flight) runs.
    """
    started_event: dict | None = None
    completed_event: dict | None = None
    failed_event: dict | None = None
    phase_events: list[dict] = []
    tool_events: list[dict] = []
    error_events: list[dict] = []

    for event in events:
        etype = str(event.get("type") or "")
        if etype == "run_skill_started":
            started_event = event
        elif etype in ("run_skill_completed", "workflow_finished"):
            completed_event = event
        elif etype in ("run_skill_failed", "workflow_failed"):
            failed_event = event
            completed_event = event
        elif etype in (
            "skill_node_started", "skill_node_completed",
            "workflow_phase_started", "workflow_phase_completed",
        ):
            phase_events.append(event)
        elif etype == "tool_executed":
            tool_events.append(event)
        elif etype in ("skill_node_failed", "workflow_phase_failed", "error"):
            error_events.append(event)

    # Incomplete run (still in-flight) — skip
    if completed_event is None:
        return None

    # Extract skill name
    skill_name = (
        _get_field(started_event, "skill")
        or _get_field(completed_event, "skill")
        or "unknown"
    )

    # Skill filter
    if skill_filter and skill_name not in skill_filter:
        return {"_filtered": True}

    # Timestamps
    started_at = (
        _get_field(started_event, "started_at")
        or (str(started_event.get("timestamp") or "") if started_event else "")
        or ""
    )
    completed_at = (
        _get_field(completed_event, "completed_at")
        or str(completed_event.get("timestamp") or "")
        or ""
    )

    # Since filter (compare completed_at)
    if since_dt is not None and completed_at:
        c_dt = _parse_iso_safe(completed_at)
        if c_dt and c_dt < since_dt:
            return {"_filtered": True}

    # Status
    status = "success"
    if failed_event is not None:
        status = "failed"
    else:
        status_raw = str(_get_field(completed_event, "status") or "success")
        if "fail" in status_raw.lower():
            status = "failed"
        elif "abort" in status_raw.lower():
            status = "aborted"

    # Duration
    duration_seconds: float | None = None
    if started_at and completed_at:
        try:
            s_dt = _parse_iso_safe(started_at)
            c_dt2 = _parse_iso_safe(completed_at)
            if s_dt and c_dt2:
                duration_seconds = max(0.0, (c_dt2 - s_dt).total_seconds())
        except Exception:
            pass

    # Phase chain
    phase_names = _extract_phase_chain(phase_events)

    # Errors (max 3)
    errors = _extract_errors(error_events, failed_event)[:3]

    # skill_version_hash (FP-0006 A compat — falls back to "unknown")
    skill_version_hash = str(
        _get_field(started_event, "skill_version_hash") or "unknown"
    )

    # Caller: extract from run_id prefix if present, else "direct"
    caller = _extract_caller(run_id, started_event)

    # Build text content
    text = _build_text(
        skill=skill_name,
        version_hash=skill_version_hash,
        caller=caller,
        status=status,
        started_at=started_at,
        ended_at=completed_at,
        duration_seconds=duration_seconds,
        phases=phase_names,
        errors=errors,
    )

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    chunk_id = f"{caller}__{run_id}" if caller != "direct" else run_id

    return {
        "id": chunk_id,
        "text": text,
        "metadata": {
            # ChunkMetadata-compatible fields
            "source_path": source_file,
            "source_type": "p6_event_run",
            "content_hash": content_hash,
            "embedding_model": "",   # filled in by embed op
            "chunk_index": 0,        # set by caller
            "size_tokens": _approx_tokens(text),
            "parent_context": None,
            "extra": {
                "skill": skill_name,
                "skill_version_hash": skill_version_hash,
                "status": status,
                "started_at": started_at,
                "ended_at": completed_at,
                "duration_seconds": duration_seconds,
                "caller": caller,
                "errors": errors,
                "phases": phase_names,
            },
        },
    }


def _build_text(
    skill: str,
    version_hash: str,
    caller: str,
    status: str,
    started_at: str,
    ended_at: str,
    duration_seconds: float | None,
    phases: list[str],
    errors: list[str],
) -> str:
    """Build the human-readable text chunk for embedding.

    Format matches the spec:
        skill: <name>
        version_hash: <hash>
        caller: <caller>
        status: <status>
        started: <ts>
        duration_seconds: <n>
        phases: a → b → c
        errors: []
        key_events:
        - ...
    """
    dur_str = f"{duration_seconds:.1f}" if duration_seconds is not None else "unknown"
    phases_str = " → ".join(phases) if phases else "(unknown)"
    errors_str = str(errors) if errors else "[]"

    lines = [
        f"skill: {skill}",
        f"version_hash: {version_hash[:12] if version_hash not in ('unknown', '') else version_hash}",
        f"caller: {caller}",
        f"status: {status}",
        f"started: {started_at}",
        f"duration_seconds: {dur_str}",
        f"phases: {phases_str}",
        f"errors: {errors_str}",
    ]

    if errors:
        lines.append("key_events:")
        for err in errors:
            lines.append(f"- error: {err[:_ERROR_EXCERPT_MAX]}")

    return "\n".join(lines)


# ── Helper functions ──────────────────────────────────────────────────────────


def _extract_run_id(event: dict) -> str:
    """Extract run_id from event.data.run_id, falling back to (skill, ts)."""
    data = event.get("data") or {}
    run_id = data.get("run_id")
    if run_id:
        return str(run_id)
    skill = data.get("skill") or "unknown"
    ts = event.get("timestamp") or event.get("ts") or ""
    return f"{skill}::{ts}"


def _get_field(event: dict | None, field: str) -> Any:
    """Get a field from event.data or event top-level."""
    if event is None:
        return None
    data = event.get("data") or {}
    return data.get(field) or event.get(field)


def _extract_phase_chain(phase_events: list[dict]) -> list[str]:
    """Return ordered unique phase names from started events."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for event in phase_events:
        etype = str(event.get("type") or "")
        if "started" in etype:
            data = event.get("data") or {}
            node = str(data.get("node") or data.get("phase") or "")
            if node and node not in seen_set:
                seen.append(node)
                seen_set.add(node)
    return seen


def _extract_errors(error_events: list[dict], failed_event: dict | None) -> list[str]:
    """Extract error messages from error events + failed event."""
    errors: list[str] = []
    seen: set[str] = set()

    def _add(msg: str) -> None:
        msg = msg.strip()[:_ERROR_EXCERPT_MAX]
        if msg and msg not in seen:
            errors.append(msg)
            seen.add(msg)

    for event in error_events:
        data = event.get("data") or {}
        msg = str(data.get("error") or data.get("message") or data.get("reason") or "")
        if msg:
            _add(msg)

    if failed_event:
        data = failed_event.get("data") or {}
        msg = str(data.get("error") or data.get("message") or data.get("reason") or "")
        if msg:
            _add(msg)

    return errors


def _extract_caller(run_id: str, started_event: dict | None) -> str:
    """Extract caller name from run_id prefix or event data."""
    if started_event:
        data = started_event.get("data") or {}
        caller = data.get("caller") or data.get("agent") or ""
        if caller:
            return str(caller)
    # Try to parse "caller__runid" pattern
    if "__" in run_id:
        return run_id.split("__", 1)[0]
    return "direct"


def _discover_event_files(events_root: str) -> list[Path]:
    """Discover all .jsonl files under events_root/**/ ."""
    root = Path(events_root)
    if not root.exists():
        return []
    pattern = str(root / "**" / "*.jsonl")
    matches = _glob_mod.glob(pattern, recursive=True)
    return [Path(m) for m in sorted(matches) if os.path.isfile(m)]


def _find_max_cursor_from_chunks() -> str | None:
    """Read the just-written event_chunks.jsonl and find the max completed_at."""
    chunks_path = Path(_CHUNKS_JSONL_PATH)
    if not chunks_path.exists():
        return None
    max_ts: str | None = None
    max_dt: datetime | None = None
    try:
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    meta = record.get("metadata") or {}
                    extra = meta.get("extra") or {}
                    completed_at = str(extra.get("ended_at") or extra.get("completed_at") or "")
                    if completed_at:
                        dt = _parse_iso_safe(completed_at)
                        if dt and (max_dt is None or dt > max_dt):
                            max_dt = dt
                            max_ts = completed_at
                except Exception:
                    continue
    except OSError:
        return None
    return max_ts


def _parse_iso_safe(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; return None on parse failure."""
    if not ts:
        return None
    ts = ts.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    return None


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)
