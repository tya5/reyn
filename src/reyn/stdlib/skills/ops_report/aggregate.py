"""aggregate.py — safe-mode aggregations for ops_report (FP-0009 Component D).

Public functions:
  collect_aggregate_fallback(artifact)                     → dict   (raw-events fallback)
  aggregate_from_raw_events(events_root, period_days, skills) → dict

The legacy ``collect_aggregate`` (= a thin back-compat wrapper that
composed ``dispatch_aggregate`` + ``collect_aggregate_fallback``) was
removed in FP-0042 Phase 2.6 — the active preprocessor chain in
``phases/collect.md`` calls the two steps directly via skill.md
``preprocessor`` entries, and the only remaining caller was the test
suite. The composition lives there now (= ``_collect_aggregate`` helper
in ``tests/test_ops_report_skill.py``).

FP-0042 Phase 2.6 (2026-05-23): migrated from mode: unsafe to mode: safe.
File reads go through ``reyn.api.safe.file``; the event-file glob covers
``.reyn/events/`` (= default-zone read), no skill.md ``file.read``
declaration needed. Path manipulation uses plain string operations
because ``pathlib`` is not on the safe-mode import allowlist.

``aggregate_from_recall_chunks`` and ``dispatch_aggregate`` live in
``aggregate_pure.py``; the two-file split is retained to keep that
module's import graph minimal (= no ``glob`` import), but both modules
are now ``mode: safe``.

R-PURE-MODE-REDEFINE wave 3a preprocessor chain (unchanged):
  1. recall run_op
  2. ``dispatch_aggregate`` in ``aggregate_pure.py`` (mode: safe)
  3. ``collect_aggregate_fallback`` (this module, mode: safe) — walks
     ``.reyn/events/*.jsonl`` only when upstream did not recall.

All functions return the same aggregate-stats output shape. No LLM calls,
no side effects beyond filesystem reads, fully testable at Tier 2.

P7 note: this module is skill-local and may freely reference event-domain
concepts (skill names, run boundaries, event type names, etc.). OS code
(op_runtime, events, kernel) does NOT import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from reyn.api.safe import file as _safe_file

# ── Constants ─────────────────────────────────────────────────────────────────

_EPOCH_ISO = "1970-01-01T00:00:00Z"
_ERROR_SAMPLE_MAX = 5
_ERROR_EXCERPT_MAX = 200

# POSIX stat-mode constants (= stat.S_IFMT / S_IFREG). Hard-coded because
# the ``stat`` module is not on the safe-mode import allowlist.
_S_IFMT = 0o170000
_S_IFREG = 0o100000


# ── Public API ────────────────────────────────────────────────────────────────


def collect_aggregate_fallback(artifact: dict) -> dict:
    """Fallback raw-events aggregator. Runs unconditionally; no-ops if upstream
    dispatcher already produced stats.

    FP-0042 Phase 2.6: mode: safe. Uses ``reyn.api.safe.file`` for event-file
    reads + stat checks; ``glob.glob`` covers path enumeration (= safe-mode
    allowlisted as a restricted ambient source per the 2026-05-15
    R-PURE-MODE stdlib audit).

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
    result.pop("_path", None)
    return result


def aggregate_from_raw_events(
    events_root: str,
    period_days: int,
    skills: list[str] | None,
) -> dict:
    """Walk events under events_root, group by run, return aggregated stats.

    Scans all ``.jsonl`` files under ``events_root`` recursively. FP-0042
    Phase 2.6: file content + stat go through ``reyn.api.safe.file``; missing
    directory or permission denial returns an empty aggregate.

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
    if not _path_exists_safe(events_root):
        return _empty_aggregate(period_days)

    cutoff = _utc_now() - timedelta(days=period_days)
    event_files = _discover_event_files(events_root)

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


def _path_exists_safe(path: str) -> bool:
    """Permission-aware existence check that does not raise.

    ``reyn.api.safe.file.exists`` raises ``PermissionError`` when the path
    falls outside the declared read zone. For the events-root probe, we
    want a permission denial to count as "not present" so the step
    degrades to an empty aggregate.
    """
    try:
        return _safe_file.exists(path)
    except (OSError, PermissionError):
        return False


def _is_regular_file(path: str) -> bool:
    """Return True iff ``path`` exists and is a regular file.

    Replacement for ``os.path.isfile`` (= ``os`` is not on the safe-mode
    allowlist). Uses ``reyn.api.safe.file.stat`` and checks the POSIX mode
    bits. Any error (missing, permission denied, broken symlink)
    returns False — matches ``os.path.isfile``'s suppress-all-errors
    behaviour.
    """
    try:
        info = _safe_file.stat(path)
    except (OSError, PermissionError):
        return False
    return (int(info.get("mode", 0)) & _S_IFMT) == _S_IFREG


def _discover_event_files(events_root: str) -> list[str]:
    """Discover all .jsonl files under events_root recursively.

    Returns a list of path strings (= no pathlib, which is not on the
    safe-mode allowlist). Filters to regular files via
    :func:`_is_regular_file` to preserve the legacy ``os.path.isfile``
    behaviour (= directory matches from broad globs are dropped).
    """
    pattern = f"{events_root}/**/*.jsonl"
    matches = _glob_mod.glob(pattern, recursive=True)
    return sorted(m for m in matches if _is_regular_file(m))


def _group_events_by_run(event_files: list[str]) -> dict[str, list[dict]]:
    """Group raw events by run_id.

    Reads each event file via :mod:`reyn.api.safe.file` (= permission-gated).
    Files outside the declared read zone, or that fail to parse, are
    silently skipped — matches the legacy OSError-swallowing read loop.
    """
    runs: dict[str, list[dict]] = defaultdict(list)

    for file_path in event_files:
        try:
            content = _safe_file.read(file_path)
        except (OSError, PermissionError):
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_id = _extract_run_id(event)
            runs[run_id].append(event)

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
