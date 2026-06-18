"""trace_collector.py — safe-mode trace aggregation for skill_improver (FP-0006 C).

Public functions:
  collect_traces_fallback(artifact) → dict   (raw-events fallback)

The legacy ``collect_traces`` back-compat wrapper was removed in FP-0042
Phase 2.7 — the active preprocessor chain in
``phases/collect_traces.md`` calls the two underlying steps (=
``dispatch_traces`` in ``trace_collector_pure.py`` + this module's
``collect_traces_fallback``) directly via skill.md. The wrapper's only
caller was the test suite; the composition moved to
``tests/test_skill_improver_collect_traces.py`` as a local helper.

FP-0042 Phase 2.7 (2026-05-23): migrated from mode: unsafe to mode: safe.
File reads + stat go through ``reyn.api.safe.file``; the
``.reyn/events/**/*.jsonl`` walk uses ``glob.glob`` (= restricted ambient
source per the 2026-05-15 R-PURE-MODE stdlib audit). Path manipulation
uses plain string operations because ``pathlib`` is not on the safe-mode
import allowlist.

R-PURE-MODE-REDEFINE wave 4 preprocessor chain (unchanged):
  1. recall run_op
  2. ``dispatch_traces`` in ``trace_collector_pure.py`` (mode: safe)
  3. ``collect_traces_fallback`` (this module, mode: safe) — walks
     ``.reyn/events/**/*.jsonl`` only when upstream did not recall.

No LLM calls, no side effects beyond reading the filesystem.

P7 note: skill-local module, may reference event-domain concepts freely.
OS code (op_runtime / models / events / kernel) does NOT import from here.
"""
from __future__ import annotations

import glob as _glob_mod
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from reyn.api.safe import file as _safe_file

# ── Constants ─────────────────────────────────────────────────────────────────

_ERROR_EXCERPT_MAX = 200
_TOP_ERRORS_MAX = 5
_EPOCH_ISO = "1970-01-01T00:00:00Z"

# POSIX stat-mode constants (= stat.S_IFMT / S_IFREG). Hard-coded because
# the ``stat`` module is not on the safe-mode import allowlist.
_S_IFMT = 0o170000
_S_IFREG = 0o100000


# ── Public API ────────────────────────────────────────────────────────────────


def collect_traces_fallback(artifact: dict) -> dict:
    """Fallback raw-events aggregator. Runs unconditionally; no-ops if upstream
    dispatcher already produced stats.

    FP-0042 Phase 2.7: mode: safe. File reads + stat go through
    :mod:`reyn.api.safe.file`.

    If ``data.traces_summary._path == "recall"``, the upstream
    ``dispatch_traces`` step already computed real stats. Strip the
    sentinel and return them. If ``data.traces_summary._path ==
    "needs_fallback"`` (or traces_summary is absent), walk
    ``.reyn/events/`` directly and produce stats from raw events.

    The ``_path`` sentinel is always stripped before returning so that
    downstream consumers (plan_improvements phase, tests) see a normal
    summary dict.
    """
    data = artifact.get("data") or {}
    traces_summary = data.get("traces_summary") or {}

    if traces_summary.get("_path") == "recall":
        result = dict(traces_summary)
        result.pop("_path", None)
        return result

    # Fallback path: walk raw events.
    skill_name: str = str(
        traces_summary.get("target_skill")
        or data.get("skill_name")
        or data.get("target_skill")
        or "unknown"
    )
    lookback: int = int(
        traces_summary.get("trace_lookback_runs")
        or data.get("trace_lookback_runs")
        or 20
    )
    return _collect_from_raw_events(
        events_root=".reyn/events",
        skill_name=skill_name,
        lookback=lookback,
    )


# ── Raw-events fallback ───────────────────────────────────────────────────────
# Recall path aggregation lives in trace_collector_pure.py. This module
# only retains the I/O-dependent raw-events fallback path.


def _collect_from_raw_events(
    events_root: str,
    *,
    skill_name: str,
    lookback: int,
) -> dict:
    """Walk events_root/**/*.jsonl, group by run, return trace summary."""
    if not _path_exists_safe(events_root):
        return _empty_result(skill_name)

    event_files = _discover_event_files(events_root)
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


def _path_exists_safe(path: str) -> bool:
    """Permission-aware existence check that does not raise.

    ``reyn.api.safe.file.exists`` raises ``PermissionError`` when the path
    falls outside the declared read zone. For the events-root probe, we
    want a permission denial to count as "not present" so the step
    degrades to an empty summary.
    """
    try:
        return _safe_file.exists(path)
    except (OSError, PermissionError):
        return False


def _is_regular_file(path: str) -> bool:
    """Return True iff ``path`` exists and is a regular file.

    Replacement for ``os.path.isfile`` (= ``os`` is not on the safe-mode
    allowlist). Uses ``reyn.api.safe.file.stat`` and checks the POSIX mode
    bits. Any error (missing, permission denied, broken symlink) returns
    False — matches ``os.path.isfile``'s suppress-all-errors behaviour.
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
    behaviour (= directory matches are dropped).
    """
    pattern = f"{events_root}/**/*.jsonl"
    matches = _glob_mod.glob(pattern, recursive=True)
    return sorted(m for m in matches if _is_regular_file(m))


def _group_events_by_run(event_files: list[str]) -> dict[str, list[dict]]:
    """Group raw events by run_id.

    Reads each event file via :mod:`reyn.api.safe.file` (= permission-gated).
    Files outside the declared read zone, or that fail to parse, are
    silently skipped.
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
    phase_started: dict[str, str] = {}
    phase_completed: dict[str, str] = {}

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
    try:
        dt = datetime.fromisoformat(ts.replace("+00:00", ""))
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── Summary builders + renderer (duplicated from trace_collector_pure.py) ─────
#
# The safe-mode AST validator rejects ``reyn.stdlib.*`` cross-module imports.
# These helpers exist in identical form in ``trace_collector_pure.py`` for
# the recall-path summary; duplicated here so this module's import graph
# stays inside the safe-mode allowlist. Keep both copies in sync by hand if
# they ever diverge — they are small and rarely change.


def _empty_result(skill_name: str) -> dict:
    """Empty trace summary for a skill with no qualifying runs."""
    summary_md = (
        f"# Traces summary for `{skill_name}`\n\n"
        "_(no run history found in the last lookback window)_\n"
    )
    return {
        "skill_name": skill_name,
        "runs_analyzed": 0,
        "data_source": "empty",
        "summary_markdown": summary_md,
        "success_rate": None,
        "top_errors": [],
    }


def _build_top_errors(error_counts: dict[str, int]) -> list[dict]:
    items = [
        {"phase": "unknown", "msg": msg, "count": cnt}
        for msg, cnt in error_counts.items()
        if cnt > 0
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items[:_TOP_ERRORS_MAX]


def _build_slow_phases(phase_duration: dict[str, list[float]]) -> list[dict]:
    result = []
    for pname, durations in phase_duration.items():
        if durations:
            avg = sum(durations) / len(durations)
            result.append({"phase": pname, "avg_seconds": avg})
    result.sort(key=lambda x: x["avg_seconds"], reverse=True)
    return result


def _build_version_section(version_hashes: dict[str, dict]) -> list[dict]:
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

    lines.append("## Top error patterns")
    if top_errors:
        for i, err in enumerate(top_errors, 1):
            phase = err.get("phase") or "unknown"
            msg = err.get("msg") or ""
            count = err.get("count") or 0
            lines.append(f'{i}. Phase `{phase}`: {count} run(s) hit "{msg[:100]}"')
    else:
        lines.append("_(no errors recorded)_")

    lines.append("")

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

    lines.append("## Skill version distribution")
    if version_section:
        for entry in version_section[:5]:
            vh = str(entry.get("hash") or "unknown")
            cnt = entry.get("count") or 0
            rate = entry.get("success_rate")
            hash_display = vh[:12] if vh not in ("unknown", "") else vh
            rate_str = (
                f"{int(rate * 100)}% success" if rate is not None else "unknown success rate"
            )
            lines.append(f"- `{hash_display}`: {cnt} runs, {rate_str}")
    else:
        lines.append("_(no version data available)_")

    lines.append("")
    return "\n".join(lines)
