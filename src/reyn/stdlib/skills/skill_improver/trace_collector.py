"""trace_collector.py — I/O-using trace aggregation for skill_improver (FP-0006 C).

Public functions:
  collect_traces(artifact)          → dict   (back-compat wrapper; prefer dispatch_traces + collect_traces_fallback)
  collect_traces_fallback(artifact) → dict   (mode: unsafe fallback; no-ops if upstream recalled)

These functions use filesystem I/O (glob, os, pathlib) and must be declared
mode: unsafe in skill.md permissions.

``aggregate_from_recall_chunks_for_traces`` and ``dispatch_traces`` have been
extracted to the sibling module ``trace_collector_pure.py`` (no unsafe imports)
so they can be declared ``mode: safe``.

R-PURE-MODE-REDEFINE wave 4: the preprocessor chain is now 3 steps:
  1. recall run_op (unchanged)
  2. ``dispatch_traces`` in ``trace_collector_pure.py`` (mode: safe) — aggregates
     recall chunks inline; returns ``{..., "_path": "recall"}`` or
     ``{"_path": "needs_fallback", ...}`` sentinel.
  3. ``collect_traces_fallback`` (this module, mode: unsafe) — no-ops when
     upstream already recalled; otherwise walks ``.reyn/events/**/*.jsonl``.

``collect_traces`` is kept as a back-compat wrapper (called by existing tests
and any code that imports it directly); it dispatches to the new two-step path.

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

# Pure helpers moved to trace_collector_pure.py (mode: safe).
# Import here so _collect_from_raw_events can use them without duplication.
from reyn.stdlib.skills.skill_improver.trace_collector_pure import (
    _build_slow_phases,
    _build_top_errors,
    _build_version_section,
    _render_summary,
)
from reyn.stdlib.skills.skill_improver.trace_collector_pure import (
    _empty_summary as _empty_result,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_ERROR_EXCERPT_MAX = 200
_EPOCH_ISO = "1970-01-01T00:00:00Z"


# ── Public API ────────────────────────────────────────────────────────────────


def collect_traces_fallback(artifact: dict) -> dict:
    """Fallback raw-events aggregator. Runs unconditionally; no-ops if upstream
    dispatcher already produced stats.

    Mode: unsafe — imports glob/os/pathlib for .reyn/events/**/*.jsonl walk.

    If ``data.traces_summary._path == "recall"``, the upstream ``dispatch_traces``
    step already computed real stats. Strip the sentinel and return them.
    If ``data.traces_summary._path == "needs_fallback"`` (or traces_summary is
    absent), walk ``.reyn/events/`` directly and produce stats from raw events.

    The ``_path`` sentinel is always stripped before returning so that downstream
    consumers (plan_improvements phase, tests) see a normal summary dict.
    """
    data = artifact.get("data") or {}
    traces_summary = data.get("traces_summary") or {}

    if traces_summary.get("_path") == "recall":
        # Upstream dispatch_traces already produced stats — just strip sentinel.
        result = dict(traces_summary)
        result.pop("_path", None)
        return result

    # Fallback path: walk raw events.
    # Pull target_skill / trace_lookback_runs from the "needs_fallback" sentinel
    # if present, or fall back to the top-level data fields (for callers that
    # skip dispatch, e.g. the back-compat wrapper).
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


def collect_traces(artifact: dict) -> dict:
    """Back-compat wrapper: dispatches to dispatch_traces then collect_traces_fallback.

    Kept so that existing tests and any direct callers continue to work.
    New preprocessor chains should use the 3-step split in collect_traces.md instead.

    Decision logic:
      - if recall produced ≥1 chunk → pure inline aggregation (mode: safe path)
      - else → raw events walk (mode: unsafe fallback path)
    """
    import copy

    from reyn.stdlib.skills.skill_improver.trace_collector_pure import dispatch_traces

    # Step 1: pure dispatch — produces either recall stats or needs_fallback sentinel.
    dispatched = dispatch_traces(artifact)

    # Inject dispatched result as data.traces_summary so collect_traces_fallback can read it.
    patched = copy.deepcopy(artifact)
    data = patched.setdefault("data", {})
    data["traces_summary"] = dispatched

    # Step 2: fallback step (no-ops if dispatched._path == "recall").
    return collect_traces_fallback(patched)


# ── Raw-events fallback ───────────────────────────────────────────────────────
# Recall path aggregation has moved to trace_collector_pure.py (mode: safe).
# This module retains only the I/O-dependent raw-events fallback path.


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
