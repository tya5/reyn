"""aggregate.py — I/O-using aggregations for ops_report (FP-0009 Component D).

Public functions:
  collect_aggregate(artifact)                              → dict   (back-compat wrapper; prefer dispatch_aggregate + collect_aggregate_fallback)
  collect_aggregate_fallback(artifact)                     → dict   (mode: unsafe fallback; no-ops if upstream recalled)
  aggregate_from_raw_events(events_root, period_days, skills) → dict

These functions use filesystem I/O (glob, os, pathlib) and must be declared
``mode: unsafe`` in skill.md.

``aggregate_from_recall_chunks`` has been extracted to the sibling module
``aggregate_pure.py`` (no unsafe imports) so it can be declared ``mode: safe``.

R-PURE-MODE-REDEFINE wave 3a: the preprocessor chain is now 3 steps:
  1. recall run_op (unchanged)
  2. ``dispatch_aggregate`` in ``aggregate_pure.py`` (mode: safe) — aggregates
     recall chunks inline; returns ``{..., "_path": "recall"}`` or
     ``{"_path": "needs_fallback", ...}`` sentinel.
  3. ``collect_aggregate_fallback`` (this module, mode: unsafe) — no-ops when
     upstream already recalled; otherwise walks ``.reyn/events/*.jsonl``.

``collect_aggregate`` is kept as a back-compat wrapper (called by existing tests
and any code that imports it directly); it dispatches to the new two-step path.

All functions return the same aggregate-stats output shape. No LLM calls,
no side effects beyond filesystem reads, fully testable at Tier 2.

P7 note: this module is skill-local and may freely reference event-domain
concepts (skill names, run boundaries, event type names, etc.). OS code
(op_runtime, events, kernel) does NOT import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

_EPOCH_ISO = "1970-01-01T00:00:00Z"
_ERROR_SAMPLE_MAX = 5
_ERROR_EXCERPT_MAX = 200


# ── Public API ────────────────────────────────────────────────────────────────


def collect_aggregate_fallback(artifact: dict) -> dict:
    """Fallback raw-events aggregator. Runs unconditionally; no-ops if upstream
    dispatcher already produced stats.

    Mode: unsafe — imports glob/os/pathlib for .reyn/events/*.jsonl walk.

    If ``data.aggregate._path == "recall"``, the upstream ``dispatch_aggregate``
    step already computed real stats. Strip the sentinel and return them.
    If ``data.aggregate._path == "needs_fallback"`` (or aggregate is absent),
    walk ``.reyn/events/`` directly and produce stats from raw events.

    The ``_path`` sentinel is always stripped before returning so that downstream
    consumers (summarize phase, tests) see a normal aggregate dict.
    """
    data = artifact.get("data") or {}
    aggregate = data.get("aggregate") or {}

    if aggregate.get("_path") == "recall":
        # Upstream dispatch_aggregate already produced stats — just strip sentinel.
        result = dict(aggregate)
        result.pop("_path", None)
        return result

    # Fallback path: walk raw events.
    # Pull period_days / skills from the "needs_fallback" sentinel if present,
    # or fall back to the top-level data fields (for callers that skip dispatch).
    period_days = int(aggregate.get("period_days") or data.get("period_days") or 7)
    skills_raw = aggregate.get("skills") if "skills" in aggregate else data.get("skills")
    skills: list[str] | None = list(skills_raw) if isinstance(skills_raw, list) else None
    result = aggregate_from_raw_events(
        events_root=".reyn/events",
        period_days=period_days,
        skills=skills,
    )
    result["_path"] = "raw_events"  # for debuggability; stripped below
    result.pop("_path", None)
    return result


def collect_aggregate(artifact: dict) -> dict:
    """Back-compat wrapper: dispatches to dispatch_aggregate then collect_aggregate_fallback.

    Kept so that existing tests and any direct callers continue to work.
    New preprocessor chains should use the 3-step split in collect.md instead.

    Decision logic:
      - if recall produced ≥1 chunk → pure inline aggregation (mode: safe path)
      - else → ``aggregate_from_raw_events`` (= fallback: walk
        ``.reyn/events/*.jsonl`` directly; index not built or empty)
    """
    from reyn.stdlib.skills.ops_report.aggregate_pure import dispatch_aggregate

    # Step 1: pure dispatch — produces either recall stats or needs_fallback sentinel.
    dispatched = dispatch_aggregate(artifact)

    # Inject dispatched result as data.aggregate so collect_aggregate_fallback can read it.
    import copy
    patched = copy.deepcopy(artifact)
    data = patched.setdefault("data", {})
    data["aggregate"] = dispatched

    # Step 2: fallback step (no-ops if dispatched._path == "recall").
    return collect_aggregate_fallback(patched)


def aggregate_from_raw_events(
    events_root: str,
    period_days: int,
    skills: list[str] | None,
) -> dict:
    """Walk events under events_root, group by run, return aggregated stats.

    Scans all .jsonl files under *events_root* (non-recursively checks
    events_root itself and one level of sub-dirs, as the P6 layout is
    typically flat or date-partitioned).

    Args:
        events_root: Path to the events directory (e.g. ".reyn/events").
                     Non-existent directory returns empty aggregate.
        period_days: Only include runs whose completed_at is within the
                     last *period_days* days from now (UTC).
        skills:      If non-None, only include runs for these skill names.

    Returns:
        {
            "total_runs":          int,
            "success_count":       int,
            "failure_count":       int,
            "success_rate":        float | None,  # None if total_runs == 0
            "period_days":         int,
            "by_skill":            dict[str, {count, success, failure, avg_duration_seconds}],
            "top_failing_skills":  list[{skill, failure_count, total_count}],
            "errors_sample":       list[str],  # last 5 error strings
        }
    """
    root = Path(events_root)
    if not root.exists() or not root.is_dir():
        return _empty_aggregate(period_days)

    cutoff = _utc_now() - timedelta(days=period_days)
    event_files = _discover_event_files(root)

    runs = _group_events_by_run(event_files)
    return _aggregate_runs(runs, cutoff=cutoff, skills=skills, period_days=period_days)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _empty_aggregate(period_days: int) -> dict:
    return {
        "total_runs": 0,
        "success_count": 0,
        "failure_count": 0,
        "success_rate": None,
        "period_days": period_days,
        "by_skill": {},
        "top_failing_skills": [],
        "errors_sample": [],
    }


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


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
    """Extract run_id from event, falling back to a composite key."""
    data = event.get("data") or {}
    run_id = data.get("run_id")
    if run_id:
        return str(run_id)
    skill = data.get("skill") or "unknown"
    ts = event.get("timestamp") or event.get("ts") or ""
    return f"{skill}::{ts}"


def _aggregate_runs(
    runs: dict[str, list[dict]],
    cutoff: datetime,
    skills: list[str] | None,
    period_days: int,
) -> dict:
    """Aggregate grouped run events into stats dict."""
    by_skill: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "success": 0,
        "failure": 0,
        "_duration_sum": 0.0,
        "_duration_count": 0,
    })
    total_runs = 0
    success_count = 0
    failure_count = 0
    errors_sample: list[str] = []

    for _run_id, events in runs.items():
        run_info = _extract_run_info(events)
        if run_info is None:
            # Incomplete run (no completion event) — skip
            continue

        skill_name = run_info["skill"]
        status = run_info["status"]
        completed_at_str = run_info["completed_at"]
        duration = run_info["duration_seconds"]
        errors = run_info["errors"]

        # Skills filter
        if skills is not None and skill_name not in skills:
            continue

        # Period filter
        if completed_at_str:
            completed_dt = _parse_iso_safe(completed_at_str)
            if completed_dt and completed_dt < cutoff:
                continue

        total_runs += 1
        is_success = status == "success"
        is_failure = status in ("failed", "aborted")

        if is_success:
            success_count += 1
        elif is_failure:
            failure_count += 1

        sk = by_skill[skill_name]
        sk["count"] += 1
        if is_success:
            sk["success"] += 1
        elif is_failure:
            sk["failure"] += 1

        if duration is not None:
            sk["_duration_sum"] += float(duration)
            sk["_duration_count"] += 1

        for err in errors:
            if len(errors_sample) < _ERROR_SAMPLE_MAX:
                errors_sample.append(str(err)[:_ERROR_EXCERPT_MAX])

    # Clean up per-skill avg_duration
    by_skill_clean = {}
    for name, stats in by_skill.items():
        avg_dur = (
            stats["_duration_sum"] / stats["_duration_count"]
            if stats["_duration_count"] > 0
            else None
        )
        by_skill_clean[name] = {
            "count": stats["count"],
            "success": stats["success"],
            "failure": stats["failure"],
            "avg_duration_seconds": avg_dur,
        }

    success_rate = success_count / total_runs if total_runs > 0 else None
    top_failing = _top_failing_skills(by_skill_clean)

    return {
        "total_runs": total_runs,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": success_rate,
        "period_days": period_days,
        "by_skill": by_skill_clean,
        "top_failing_skills": top_failing,
        "errors_sample": errors_sample,
    }


def _extract_run_info(events: list[dict]) -> dict[str, Any] | None:
    """Extract key run info from a list of events for one run.

    Returns None if the run is incomplete (no completion event).
    """
    started_event: dict | None = None
    completed_event: dict | None = None
    failed_event: dict | None = None
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
        elif etype in ("skill_node_failed", "workflow_phase_failed", "error"):
            error_events.append(event)

    # Incomplete run: no completion event
    if completed_event is None:
        return None

    def _get_field(ev: dict | None, field: str) -> Any:
        if ev is None:
            return None
        data = ev.get("data") or {}
        return data.get(field) or ev.get(field)

    skill_name = (
        _get_field(started_event, "skill")
        or _get_field(completed_event, "skill")
        or "unknown"
    )

    started_at = (
        _get_field(started_event, "started_at")
        or (str(started_event.get("timestamp") or "") if started_event else "")
    )
    completed_at = (
        _get_field(completed_event, "completed_at")
        or str(completed_event.get("timestamp") or "")
    )

    # Status
    if failed_event is not None:
        status = "failed"
    else:
        status_raw = str(_get_field(completed_event, "status") or "success").lower()
        if "fail" in status_raw:
            status = "failed"
        elif "abort" in status_raw:
            status = "aborted"
        else:
            status = "success"

    # Duration
    duration_seconds: float | None = None
    if started_at and completed_at:
        s_dt = _parse_iso_safe(started_at)
        c_dt = _parse_iso_safe(completed_at)
        if s_dt and c_dt:
            duration_seconds = max(0.0, (c_dt - s_dt).total_seconds())

    # Errors
    errors: list[str] = []
    for ev in error_events:
        data = ev.get("data") or {}
        msg = str(data.get("message") or data.get("msg") or data.get("error") or "")
        if msg:
            errors.append(msg)

    return {
        "skill": skill_name,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "errors": errors,
    }


def _top_failing_skills(by_skill: dict[str, dict]) -> list[dict]:
    """Return skills sorted by failure_count descending."""
    failing = [
        {
            "skill": name,
            "failure_count": stats["failure"],
            "total_count": stats["count"],
        }
        for name, stats in by_skill.items()
        if stats["failure"] > 0
    ]
    failing.sort(key=lambda x: x["failure_count"], reverse=True)
    return failing


def _parse_iso_safe(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp, returning None on failure."""
    if not ts:
        return None
    ts = ts.strip()
    # Normalize trailing Z → +00:00
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        pass
    # Try without timezone info (treat as UTC)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", ""))
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
