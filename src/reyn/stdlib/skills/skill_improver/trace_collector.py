"""trace_collector.py — pure-function trace aggregation for skill_improver (FP-0006 C).

Public function:
  collect_traces(artifact) → dict   (preprocessor python step entry point)

Decision:
  - if data.trace_recall_result has ≥1 chunk → recall path (filter by skill_name)
  - else → walk .reyn/events/**/*.jsonl (raw-events fallback)
  - if neither → return empty summary

No LLM calls, no side effects beyond reading the filesystem. Fully testable at Tier 2.

P7 note: skill-local module, may reference event-domain concepts freely.
OS code (op_runtime / models / events / kernel) does NOT import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

_ERROR_EXCERPT_MAX = 200
_TOP_ERRORS_MAX = 5
_EPOCH_ISO = "1970-01-01T00:00:00Z"


# ── Public API ────────────────────────────────────────────────────────────────


def collect_traces(artifact: dict) -> dict:
    """Preprocessor python step entry point.

    Reads inputs from ``artifact["data"]``:
      - ``skill_name``:           target skill being improved (str)
      - ``improvement_source``:   "tests" | "traces" | "both"
      - ``trace_lookback_runs``:  int (default 20)
      - ``trace_recall_result``:  recall op output or None (on_error: skip)

    Decision:
      - recall path when recall result has ≥1 chunk
      - raw-events fallback when recall has 0 chunks / is None
      - empty result when no data anywhere

    Returns:
        {
            "skill_name":       str,
            "runs_analyzed":    int,
            "data_source":      "recall" | "raw_events" | "empty",
            "summary_markdown": str,
            "success_rate":     float | None,
            "top_errors":       [{"phase": str, "msg": str, "count": int}],
        }
    """
    data = artifact.get("data") or {}
    skill_name: str = str(data.get("skill_name") or data.get("target_skill") or "unknown")
    lookback: int = int(data.get("trace_lookback_runs") or 20)

    recall_result = data.get("trace_recall_result")
    chunks: list[dict] = []
    if isinstance(recall_result, dict):
        raw = recall_result.get("chunks")
        if isinstance(raw, list):
            chunks = raw

    if chunks:
        return _collect_from_recall(chunks, skill_name=skill_name, lookback=lookback)

    # Fallback: raw events walk
    return _collect_from_raw_events(
        events_root=".reyn/events",
        skill_name=skill_name,
        lookback=lookback,
    )


# ── Recall path ───────────────────────────────────────────────────────────────


def _collect_from_recall(
    chunks: list[dict],
    *,
    skill_name: str,
    lookback: int,
) -> dict:
    """Aggregate from recall chunks, filtering to skill_name and capping at lookback."""
    # Filter to the target skill
    filtered = [
        c for c in chunks
        if str((c.get("metadata") or {}).get("extra", {}).get("skill") or "") == skill_name
    ]

    # Cap to lookback (recall returns top-k already, but honour the cap)
    filtered = filtered[:lookback]

    if not filtered:
        # No data for this skill in recall index
        return _empty_result(skill_name)

    total = len(filtered)
    success_count = 0
    failure_count = 0
    error_counts: dict[str, int] = defaultdict(int)
    phase_duration: dict[str, list[float]] = defaultdict(list)
    version_hashes: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "success": 0})

    for chunk in filtered:
        meta = chunk.get("metadata") or {}
        extra = meta.get("extra") or {}

        status = str(extra.get("status") or "unknown").lower()
        is_success = status == "success"
        is_failure = status in ("failed", "aborted")

        if is_success:
            success_count += 1
        elif is_failure:
            failure_count += 1

        # Error aggregation
        errors: list[str] = list(extra.get("errors") or [])
        for err in errors:
            key = str(err)[:_ERROR_EXCERPT_MAX]
            error_counts[key] += 1

        # Phase durations (from "phases" list in extra — list of visited phase names)
        phases_visited: list[str] = list(extra.get("phases") or [])
        dur = extra.get("duration_seconds")
        if dur is not None and phases_visited:
            # attribute duration to last phase in the chain (approx)
            try:
                phase_duration[phases_visited[-1]].append(float(dur))
            except (TypeError, ValueError):
                pass

        # Version hash distribution
        vh = str(extra.get("skill_version_hash") or "unknown")
        version_hashes[vh]["count"] += 1
        if is_success:
            version_hashes[vh]["success"] += 1

    success_rate = success_count / total if total > 0 else None
    top_errors = _build_top_errors(error_counts)
    slow_phases = _build_slow_phases(phase_duration)
    version_section = _build_version_section(version_hashes)

    summary_md = _render_summary(
        skill_name=skill_name,
        runs_analyzed=total,
        data_source="recall",
        success_count=success_count,
        failure_count=failure_count,
        success_rate=success_rate,
        top_errors=top_errors,
        slow_phases=slow_phases,
        version_section=version_section,
    )

    return {
        "skill_name": skill_name,
        "runs_analyzed": total,
        "data_source": "recall",
        "summary_markdown": summary_md,
        "success_rate": success_rate,
        "top_errors": top_errors,
    }


# ── Raw-events fallback ───────────────────────────────────────────────────────


def _collect_from_raw_events(
    events_root: str,
    *,
    skill_name: str,
    lookback: int,
) -> dict:
    """Walk events_root/**/*.jsonl, group by run, return trace summary."""
    root = Path(events_root)
    if not root.exists() or not root.is_dir():
        return _empty_result(skill_name)

    event_files = _discover_event_files(root)
    if not event_files:
        return _empty_result(skill_name)

    runs = _group_events_by_run(event_files)

    # Extract run infos, filter by skill, sort newest-first, cap at lookback
    run_infos: list[dict] = []
    for _run_id, events in runs.items():
        info = _extract_run_info(events)
        if info is None:
            continue
        if info["skill"] != skill_name:
            continue
        run_infos.append(info)

    # Sort by completed_at descending (newest first), then take last `lookback`
    run_infos.sort(key=lambda r: r.get("completed_at") or "", reverse=True)
    run_infos = run_infos[:lookback]

    if not run_infos:
        return _empty_result(skill_name)

    total = len(run_infos)
    success_count = 0
    failure_count = 0
    error_counts: dict[str, int] = defaultdict(int)
    phase_duration: dict[str, list[float]] = defaultdict(list)
    version_hashes: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "success": 0})

    for info in run_infos:
        status = info.get("status") or "unknown"
        is_success = status == "success"
        is_failure = status in ("failed", "aborted")

        if is_success:
            success_count += 1
        elif is_failure:
            failure_count += 1

        for err in info.get("errors") or []:
            key = str(err)[:_ERROR_EXCERPT_MAX]
            error_counts[key] += 1

        # Phase-level durations
        for phase_name, dur in (info.get("phase_durations") or {}).items():
            if dur is not None:
                try:
                    phase_duration[phase_name].append(float(dur))
                except (TypeError, ValueError):
                    pass

        vh = str(info.get("skill_version_hash") or "unknown")
        version_hashes[vh]["count"] += 1
        if is_success:
            version_hashes[vh]["success"] += 1

    success_rate = success_count / total if total > 0 else None
    top_errors = _build_top_errors(error_counts)
    slow_phases = _build_slow_phases(phase_duration)
    version_section = _build_version_section(version_hashes)

    summary_md = _render_summary(
        skill_name=skill_name,
        runs_analyzed=total,
        data_source="raw_events",
        success_count=success_count,
        failure_count=failure_count,
        success_rate=success_rate,
        top_errors=top_errors,
        slow_phases=slow_phases,
        version_section=version_section,
    )

    return {
        "skill_name": skill_name,
        "runs_analyzed": total,
        "data_source": "raw_events",
        "summary_markdown": summary_md,
        "success_rate": success_rate,
        "top_errors": top_errors,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────


def _empty_result(skill_name: str) -> dict:
    summary_md = (
        f"# Traces summary for `{skill_name}`\n\n"
        "**Runs analyzed**: 0\n\n"
        "No historical execution data found for this skill. "
        "Improvement plan will rely on test scores only.\n"
    )
    return {
        "skill_name": skill_name,
        "runs_analyzed": 0,
        "data_source": "empty",
        "summary_markdown": summary_md,
        "success_rate": None,
        "top_errors": [],
    }


def _discover_event_files(root: Path) -> list[Path]:
    """Discover all .jsonl files under root recursively."""
    pattern = str(root / "**" / "*.jsonl")
    matches = _glob_mod.glob(pattern, recursive=True)
    return [Path(m) for m in sorted(matches) if os.path.isfile(m)]


def _group_events_by_run(event_files: list[Path]) -> dict[str, list[dict]]:
    """Group raw events by run_id."""
    runs: dict[str, list[dict]] = defaultdict(list)
    for file_path in event_files:
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
        except OSError:
            continue
    return runs


def _extract_run_id(event: dict) -> str:
    """Extract run_id from event.data.run_id, falling back to (skill, ts)."""
    data = event.get("data") or {}
    run_id = data.get("run_id")
    if run_id:
        return str(run_id)
    skill = data.get("skill") or "unknown"
    ts = event.get("timestamp") or event.get("ts") or ""
    return f"{skill}::{ts}"


def _extract_run_info(events: list[dict]) -> dict | None:
    """Extract key info from a list of events for one run.

    Returns None for incomplete runs (no completion event).
    """
    started_event: dict | None = None
    completed_event: dict | None = None
    failed_event: dict | None = None
    error_events: list[dict] = []
    phase_started: dict[str, str] = {}  # phase_name → started_at ISO
    phase_completed: dict[str, str] = {}  # phase_name → completed_at ISO

    for event in events:
        etype = str(event.get("type") or "")
        if etype == "run_skill_started":
            started_event = event
        elif etype in ("run_skill_completed", "workflow_finished"):
            completed_event = event
        elif etype in ("run_skill_failed", "workflow_failed"):
            failed_event = event
            completed_event = event
        elif etype in ("skill_node_failed", "workflow_phase_failed", "error"):
            error_events.append(event)
        elif etype in ("skill_node_started", "workflow_phase_started"):
            data = event.get("data") or {}
            node = str(data.get("node") or data.get("phase") or "")
            ts = str(event.get("timestamp") or "")
            if node and ts:
                phase_started[node] = ts
        elif etype in ("skill_node_completed", "workflow_phase_completed"):
            data = event.get("data") or {}
            node = str(data.get("node") or data.get("phase") or "")
            ts = str(event.get("timestamp") or "")
            if node and ts:
                phase_completed[node] = ts

    if completed_event is None:
        return None

    def _gf(ev: dict | None, field: str) -> Any:
        if ev is None:
            return None
        data = ev.get("data") or {}
        return data.get(field) or ev.get(field)

    skill_name = str(
        _gf(started_event, "skill") or _gf(completed_event, "skill") or "unknown"
    )
    skill_version_hash = str(_gf(started_event, "skill_version_hash") or "unknown")

    started_at = str(
        _gf(started_event, "started_at")
        or (started_event.get("timestamp") if started_event else "")
        or ""
    )
    completed_at = str(
        _gf(completed_event, "completed_at")
        or completed_event.get("timestamp")
        or ""
    )

    if failed_event is not None:
        status = "failed"
    else:
        raw = str(_gf(completed_event, "status") or "success").lower()
        if "fail" in raw:
            status = "failed"
        elif "abort" in raw:
            status = "aborted"
        else:
            status = "success"

    # Per-phase durations
    phase_durations: dict[str, float | None] = {}
    for pname, p_started in phase_started.items():
        p_completed = phase_completed.get(pname)
        if p_completed:
            s_dt = _parse_iso_safe(p_started)
            c_dt = _parse_iso_safe(p_completed)
            if s_dt and c_dt:
                phase_durations[pname] = max(0.0, (c_dt - s_dt).total_seconds())
            else:
                phase_durations[pname] = None
        else:
            phase_durations[pname] = None

    # Errors
    errors: list[str] = []
    for ev in error_events:
        data = ev.get("data") or {}
        msg = str(data.get("message") or data.get("msg") or data.get("error") or "")
        if msg:
            errors.append(msg)

    return {
        "skill": skill_name,
        "skill_version_hash": skill_version_hash,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "errors": errors,
        "phase_durations": phase_durations,
    }


def _build_top_errors(
    error_counts: dict[str, int],
) -> list[dict]:
    """Return top errors sorted by count descending, max _TOP_ERRORS_MAX."""
    items = [
        {"phase": "unknown", "msg": msg, "count": cnt}
        for msg, cnt in error_counts.items()
        if cnt > 0
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items[:_TOP_ERRORS_MAX]


def _build_slow_phases(
    phase_duration: dict[str, list[float]],
) -> list[dict]:
    """Return phases sorted by average duration descending."""
    result = []
    for pname, durations in phase_duration.items():
        if durations:
            avg = sum(durations) / len(durations)
            result.append({"phase": pname, "avg_seconds": avg})
    result.sort(key=lambda x: x["avg_seconds"], reverse=True)
    return result


def _build_version_section(
    version_hashes: dict[str, dict[str, Any]],
) -> list[dict]:
    """Return version hash distribution sorted by count descending."""
    result = []
    for vh, stats in version_hashes.items():
        cnt = stats["count"]
        success = stats["success"]
        rate = success / cnt if cnt > 0 else None
        result.append({"hash": vh, "count": cnt, "success_rate": rate})
    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def _render_summary(
    *,
    skill_name: str,
    runs_analyzed: int,
    data_source: str,
    success_count: int,
    failure_count: int,
    success_rate: float | None,
    top_errors: list[dict],
    slow_phases: list[dict],
    version_section: list[dict],
) -> str:
    """Render the traces_summary.md markdown string."""
    lines = [
        f"# Traces summary for `{skill_name}`",
        "",
        f"**Runs analyzed**: {runs_analyzed} (data source: {data_source})",
        f"**Window**: last {runs_analyzed} invocations",
    ]

    if success_rate is not None:
        pct = int(success_rate * 100)
        lines.append(f"**Success rate**: {pct}% ({success_count}/{runs_analyzed})")
    else:
        lines.append("**Success rate**: n/a")

    lines.append("")

    # Top error patterns
    lines.append("## Top error patterns")
    if top_errors:
        for i, err in enumerate(top_errors, 1):
            phase = err.get("phase") or "unknown"
            msg = err.get("msg") or ""
            count = err.get("count") or 0
            lines.append(f"{i}. Phase `{phase}`: {count} run(s) hit \"{msg[:100]}\"")
    else:
        lines.append("_(no errors recorded)_")

    lines.append("")

    # Slowest phases
    lines.append("## Slowest phases")
    if slow_phases:
        for entry in slow_phases[:5]:
            pname = entry.get("phase") or "unknown"
            avg = entry.get("avg_seconds")
            if avg is not None:
                lines.append(f"- `{pname}`: avg {avg:.1f}s")
    else:
        lines.append("_(no phase timing data available)_")

    lines.append("")

    # Skill version distribution
    lines.append("## Skill version distribution")
    if version_section:
        for entry in version_section[:5]:
            vh = str(entry.get("hash") or "unknown")
            cnt = entry.get("count") or 0
            rate = entry.get("success_rate")
            hash_display = vh[:12] if vh not in ("unknown", "") else vh
            rate_str = f"{int(rate * 100)}% success" if rate is not None else "unknown success rate"
            lines.append(f"- `{hash_display}`: {cnt} runs, {rate_str}")
    else:
        lines.append("_(no version data available)_")

    lines.append("")
    return "\n".join(lines)


def _parse_iso_safe(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; return None on failure."""
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass
    # Fallback without tz
    try:
        dt = datetime.fromisoformat(ts.replace("+00:00", ""))
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
