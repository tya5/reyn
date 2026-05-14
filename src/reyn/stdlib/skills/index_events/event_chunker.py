"""event_chunker.py — deterministic P6 event log chunker for index_events.

All public functions are either:
  (a) phase preprocessor steps (receive the full artifact dict, return JSON-
      serializable value placed at an `into` path), or
  (b) postprocessor python steps (same calling convention).

No LLM calls are made here. All logic is deterministic.

P7 note: this module is skill-local and may freely reference event-domain
concepts (skill names, run boundaries, tool_executed, etc.). OS code
(op_runtime, events, kernel) does NOT import from here.
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

# ── Constants ────────────────────────────────────────────────────────────────

_CURSOR_FILE = Path(".reyn") / "index" / "events_cursor"
_EVENTS_DIR = Path(".reyn") / "events"
_CHUNKS_JSONL_PATH = "artifacts/event_chunks.jsonl"
_EPOCH_ISO = "1970-01-01T00:00:00Z"
_ERROR_EXCERPT_MAX = 200


# ── Phase preprocessor step ──────────────────────────────────────────────────


def resolve_scan_context(artifact: dict) -> dict:
    """Phase preprocessor: resolve cursor + discover event files.

    Receives the full index_events_input artifact. Reads the cursor file
    (if present) to determine the effective lower-bound timestamp, then
    discovers all .jsonl files under .reyn/events/.

    Returns:
        {
            "since":          str,          # effective ISO-8601 lower bound
            "event_files":    list[str],    # all discovered .jsonl paths
            "cursor_exists":  bool,
            "cursor_value":   str | null,
        }
    """
    data = artifact.get("data") or {}
    since_input: str | None = data.get("since")
    mode: str = str(data.get("mode") or "append")

    cursor_exists = _CURSOR_FILE.exists()
    cursor_value: str | None = None

    if mode == "replace":
        # Full reindex — ignore cursor
        since = _EPOCH_ISO
    elif since_input:
        since = since_input
    elif cursor_exists:
        try:
            cursor_value = _CURSOR_FILE.read_text(encoding="utf-8").strip()
            since = cursor_value if cursor_value else _EPOCH_ISO
        except OSError:
            since = _EPOCH_ISO
    else:
        since = _EPOCH_ISO

    event_files = _discover_event_files()
    return {
        "since": since,
        "event_files": event_files,
        "cursor_exists": cursor_exists,
        "cursor_value": cursor_value,
    }


# ── Postprocessor python steps ────────────────────────────────────────────────


def build_chunks(artifact: dict) -> dict:
    """Postprocessor python step: chunk event files into run-unit chunks.

    Receives the LLM's finish artifact (= scan_plan). Streams each event
    file line by line, groups events by run_id, emits one chunk per complete
    run, writes to artifacts/event_chunks.jsonl.

    Returns a summary dict placed at `data.chunk_stats`:
        {
            "chunk_count":    int,    # complete runs indexed
            "skipped_runs":   int,    # incomplete runs (no completion event)
            "filtered_runs":  int,    # runs excluded by since/skill_filter
        }
    """
    data = artifact.get("data") or {}
    since_str: str = str(data.get("since") or _EPOCH_ISO)
    event_files: list[str] = list(data.get("event_files") or [])
    skill_filter_raw = data.get("skill_filter")
    skill_filter: list[str] | None = list(skill_filter_raw) if skill_filter_raw else None

    since_dt = _parse_iso(since_str)
    file_paths = [Path(f) for f in event_files]

    output_path = Path(_CHUNKS_JSONL_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_count = 0
    skipped_runs = 0
    filtered_runs = 0

    with open(output_path, "w", encoding="utf-8") as out_f:
        for chunk in chunk_runs(file_paths, since_dt, skill_filter):
            record = {
                "text": chunk["content"],
                "metadata": chunk["metadata"],
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            chunk_count += 1

        # Collect stats from the generator's internal state by replaying
        # (We collect stats inline using the streaming version below)

    # Re-run with stats collection for skipped/filtered counts
    # (chunk_runs is a generator; re-run is cheap for correctness)
    output_path2 = Path(_CHUNKS_JSONL_PATH + ".tmp")
    chunk_count2 = 0
    skipped_runs2 = 0
    filtered_runs2 = 0

    with open(output_path2, "w", encoding="utf-8") as out_f2:
        for result in _chunk_runs_with_stats(file_paths, since_dt, skill_filter):
            if result["kind"] == "chunk":
                record = {
                    "text": result["content"],
                    "metadata": result["metadata"],
                }
                out_f2.write(json.dumps(record, ensure_ascii=False) + "\n")
                chunk_count2 += 1
            elif result["kind"] == "skipped":
                skipped_runs2 += 1
            elif result["kind"] == "filtered":
                filtered_runs2 += 1

    # Replace the initial (chunked-only) output with the stats-aware output
    os.replace(str(output_path2), str(output_path))

    return {
        "chunk_count": chunk_count2,
        "skipped_runs": skipped_runs2,
        "filtered_runs": filtered_runs2,
    }


def update_cursor(artifact: dict) -> dict:
    """Postprocessor python step: update .reyn/index/events_cursor.

    Receives the full artifact after build_chunks. Finds the maximum
    completed_at across indexed runs (from chunk_stats context) and writes
    it atomically to the cursor file.

    Returns:
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
    embed_result = data.get("embed_result") or {}
    index_result = data.get("index_result") or {}

    indexed_runs = int(chunk_stats.get("chunk_count") or 0)
    skipped_runs = int(chunk_stats.get("skipped_runs") or 0)
    filtered_runs = int(chunk_stats.get("filtered_runs") or 0)

    # Determine new cursor from the chunks that were just written
    new_cursor = _find_max_cursor_from_chunks()
    if not new_cursor:
        # No new chunks: preserve existing cursor (or epoch if absent)
        if _CURSOR_FILE.exists():
            try:
                new_cursor = _CURSOR_FILE.read_text(encoding="utf-8").strip() or _EPOCH_ISO
            except OSError:
                new_cursor = _EPOCH_ISO
        else:
            new_cursor = _EPOCH_ISO

    _write_cursor_atomic(new_cursor)

    return {
        "indexed_runs": indexed_runs,
        "skipped_runs": skipped_runs,
        "filtered_runs": filtered_runs,
        "new_cursor": new_cursor,
        "sources_updated": ["events"],
        "chunk_stats": chunk_stats,
        "embed_result": embed_result,
        "index_result": index_result,
    }


# ── Core chunking logic (public — used in tests directly) ────────────────────


def chunk_runs(
    event_files: list[Path],
    since: datetime,
    skill_filter: list[str] | None,
) -> Iterator[dict]:
    """Yield one chunk dict per complete run.

    A "complete run" has both run_skill_started and run_skill_completed (or
    run_skill_failed) events. Incomplete runs (still running) are skipped.

    Args:
        event_files: JSONL files to stream.
        since: Only yield runs whose completed_at >= since.
        skill_filter: If non-None, only yield runs whose skill is in this list.

    Yields:
        {
            "content":  str,   # human-readable text summary (gets embedded)
            "metadata": dict,  # ChunkMetadata-compatible dict
        }
    """
    for result in _chunk_runs_with_stats(event_files, since, skill_filter):
        if result["kind"] == "chunk":
            yield {"content": result["content"], "metadata": result["metadata"]}


# ── Internal implementation ──────────────────────────────────────────────────


def _chunk_runs_with_stats(
    event_files: list[Path],
    since: datetime,
    skill_filter: list[str] | None,
) -> Iterator[dict]:
    """Internal generator yielding chunk / skipped / filtered result dicts."""
    # Group events by run_id across all files.
    # run_id → list of events (in order)
    runs: dict[str, list[dict]] = defaultdict(list)
    run_files: dict[str, str] = {}  # run_id → source file path

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
        result = _process_run(
            run_id, events, run_files.get(run_id, ""), since, skill_filter
        )
        yield result


def _extract_run_id(event: dict) -> str:
    """Extract run_id from an event, falling back to (skill, started_at) tuple."""
    data = event.get("data") or {}
    run_id = data.get("run_id")
    if run_id:
        return str(run_id)
    # Fallback: use skill name + timestamp from the event itself
    skill = data.get("skill") or "unknown"
    ts = event.get("timestamp") or event.get("ts") or ""
    return f"{skill}::{ts}"


def _process_run(
    run_id: str,
    events: list[dict],
    source_file: str,
    since: datetime,
    skill_filter: list[str] | None,
) -> dict:
    """Process a list of events for one run and return a result dict.

    Returns:
        {"kind": "chunk", "content": ..., "metadata": ...}
      | {"kind": "skipped"}   # incomplete run
      | {"kind": "filtered"}  # excluded by since/skill_filter
    """
    # Find the started + completed events
    started_event: dict | None = None
    completed_event: dict | None = None
    failed_event: dict | None = None
    phase_events: list[dict] = []
    tool_events: list[dict] = []
    error_events: list[dict] = []

    for event in events:
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        if etype == "run_skill_started":
            started_event = event
        elif etype in ("run_skill_completed", "workflow_finished"):
            completed_event = event
        elif etype in ("run_skill_failed", "workflow_failed"):
            failed_event = event
            completed_event = event  # treat as completion for chunking
        elif etype in ("skill_node_started", "skill_node_completed",
                       "workflow_phase_started", "workflow_phase_completed"):
            phase_events.append(event)
        elif etype == "tool_executed":
            tool_events.append(event)
        elif etype in ("skill_node_failed", "workflow_phase_failed", "error"):
            error_events.append(event)

    # Incomplete run — no completion event
    if completed_event is None:
        return {"kind": "skipped"}

    # Extract skill name from started or completed event
    skill_name = _get_field(started_event, "skill") or _get_field(completed_event, "skill") or "unknown"

    # Skill filter check
    if skill_filter and skill_name not in skill_filter:
        return {"kind": "filtered"}

    # Extract timestamps
    started_at = _get_field(started_event, "started_at") or _get_field(started_event, "timestamp") or ""
    if not started_at and started_event:
        started_at = str(started_event.get("timestamp") or "")
    completed_at = _get_field(completed_event, "completed_at") or _get_field(completed_event, "timestamp") or ""
    if not completed_at:
        completed_at = str(completed_event.get("timestamp") or "")

    # Timestamp filter: use completed_at for since comparison
    if completed_at:
        completed_dt = _parse_iso_safe(completed_at)
        if completed_dt and completed_dt < since:
            return {"kind": "filtered"}

    # Derive status
    status = "success"
    if failed_event is not None:
        # An explicit run_skill_failed event was emitted → status is definitively failed.
        status = "failed"
    else:
        status_raw = (_get_field(completed_event, "status") or "success").lower()
        if "abort" in status_raw:
            status = "aborted"
        elif "fail" in status_raw:
            status = "failed"

    # Duration
    duration_seconds: int | None = None
    if started_at and completed_at:
        try:
            s_dt = _parse_iso_safe(started_at)
            c_dt = _parse_iso_safe(completed_at)
            if s_dt and c_dt:
                duration_seconds = max(0, int((c_dt - s_dt).total_seconds()))
        except Exception:
            pass

    # Phase chain
    phase_names = _extract_phase_chain(phase_events, started_event, completed_event)

    # Tool call counts
    tool_calls = _count_tool_calls(tool_events)

    # Cost
    cost_usd = _get_cost(completed_event, events)

    # Errors
    errors = _extract_errors(error_events, failed_event)

    # skill_version_hash
    skill_version_hash = _get_field(started_event, "skill_version_hash") or "unknown"

    # Build content text (optimised for semantic search)
    content = _build_content(
        skill=skill_name,
        version_hash=skill_version_hash,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        status=status,
        phases=phase_names,
        tool_calls=tool_calls,
        cost_usd=cost_usd,
        errors=errors,
    )

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    metadata = {
        "source_path": source_file,
        "source_type": "p6_event_run",
        "content_hash": content_hash,
        "embedding_model": "",  # filled in by embed op
        "chunk_index": 0,
        "size_tokens": _approx_tokens(content),
        "parent_context": None,
        "extra": {
            "skill": skill_name,
            "skill_version_hash": skill_version_hash,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration_seconds,
            "status": status,
            "phases": phase_names,
            "errors": errors,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
        },
    }

    return {"kind": "chunk", "content": content, "metadata": metadata}


def _build_content(
    skill: str,
    version_hash: str,
    started_at: str,
    completed_at: str,
    duration_seconds: int | None,
    status: str,
    phases: list[str],
    tool_calls: dict[str, int],
    cost_usd: float | None,
    errors: list[str],
) -> str:
    """Build human-readable text content for embedding.

    Repeats skill name multiple times to improve semantic search recall
    (search query typically includes the skill name).
    """
    lines = [
        f"skill: {skill}",
        f"skill_name: {skill}",
        f"version_hash: {version_hash[:12] if version_hash and version_hash != 'unknown' else version_hash}",
        f"started: {started_at}",
        f"completed: {completed_at}",
        f"duration: {duration_seconds}s" if duration_seconds is not None else "duration: unknown",
        f"status: {status}",
    ]

    if phases:
        lines.append("phases: " + " → ".join(phases))
    else:
        lines.append("phases: (unknown)")

    if tool_calls:
        tc_parts = " ".join(f"{k}×{v}" for k, v in sorted(tool_calls.items()))
        lines.append(f"tool_calls: {tc_parts}")
    else:
        lines.append("tool_calls: (none)")

    if cost_usd is not None:
        lines.append(f"cost_usd: ${cost_usd:.4f}")
    else:
        lines.append("cost_usd: (unknown)")

    if errors:
        err_parts = [e[:_ERROR_EXCERPT_MAX] for e in errors]
        lines.append("errors: " + " | ".join(err_parts))
    else:
        lines.append("errors: (none)")

    # Repeat skill name at end for better recall
    lines.append(f"indexed_skill: {skill}")

    return "\n".join(lines)


# ── Helper functions ──────────────────────────────────────────────────────────


def _get_field(event: dict | None, field: str) -> Any:
    """Get a field from event.data or event directly."""
    if event is None:
        return None
    data = event.get("data") or {}
    return data.get(field) or event.get(field)


def _extract_phase_chain(
    phase_events: list[dict],
    started_event: dict | None,
    completed_event: dict | None,
) -> list[str]:
    """Extract ordered unique phase names from events."""
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


def _count_tool_calls(tool_events: list[dict]) -> dict[str, int]:
    """Count tool/op executions by name."""
    counts: dict[str, int] = defaultdict(int)
    for event in tool_events:
        data = event.get("data") or {}
        op_name = str(data.get("op") or data.get("tool") or data.get("kind") or "")
        if op_name:
            counts[op_name] += 1
    return dict(counts)


def _get_cost(completed_event: dict | None, all_events: list[dict]) -> float | None:
    """Extract total cost from completed event or sum from LLM events."""
    if completed_event:
        data = completed_event.get("data") or {}
        cost = data.get("cost_usd") or data.get("total_cost_usd")
        if cost is not None:
            try:
                return float(cost)
            except (TypeError, ValueError):
                pass

    # Fallback: sum cost fields from all events
    total = 0.0
    found = False
    for event in all_events:
        data = event.get("data") or {}
        c = data.get("cost_usd") or data.get("cost")
        if c is not None:
            try:
                total += float(c)
                found = True
            except (TypeError, ValueError):
                pass
    return total if found else None


def _extract_errors(error_events: list[dict], failed_event: dict | None) -> list[str]:
    """Extract error messages from error events."""
    errors: list[str] = []
    for event in error_events:
        data = event.get("data") or {}
        msg = str(
            data.get("error") or data.get("message") or data.get("reason") or ""
        )
        if msg:
            errors.append(msg[:_ERROR_EXCERPT_MAX])
    if failed_event:
        data = failed_event.get("data") or {}
        msg = str(data.get("error") or data.get("message") or data.get("reason") or "")
        if msg and msg not in errors:
            errors.append(msg[:_ERROR_EXCERPT_MAX])
    return errors


def _discover_event_files() -> list[str]:
    """Discover all .jsonl files under .reyn/events/."""
    if not _EVENTS_DIR.exists():
        return []
    pattern = str(_EVENTS_DIR / "**" / "*.jsonl")
    matches = _glob_mod.glob(pattern, recursive=True)
    return sorted(m for m in matches if os.path.isfile(m))


def _find_max_cursor_from_chunks() -> str | None:
    """Read the written chunks.jsonl and find the max completed_at."""
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
                    completed_at = str(extra.get("completed_at") or "")
                    if completed_at:
                        dt = _parse_iso_safe(completed_at)
                        if dt and (max_dt is None or dt > max_dt):
                            max_dt = dt
                            max_ts = completed_at
                except (json.JSONDecodeError, Exception):
                    continue
    except OSError:
        return None
    return max_ts


def _write_cursor_atomic(value: str) -> None:
    """Write cursor file atomically using tempfile + os.rename."""
    _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(_CURSOR_FILE.parent), prefix=".events_cursor_tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.rename(tmp_path, str(_CURSOR_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp to datetime (UTC-aware). Falls back to epoch."""
    dt = _parse_iso_safe(ts)
    if dt is not None:
        return dt
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_iso_safe(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; return None on parse failure."""
    if not ts:
        return None
    ts = ts.strip()
    # Normalise Z suffix
    ts_norm = ts.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts_norm, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    # Python 3.11+ fromisoformat handles more formats
    try:
        dt = datetime.fromisoformat(ts_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    return None


def _approx_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token (GPT-style BPE approximation)."""
    return max(1, len(text) // 4)
