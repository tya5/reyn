"""Pure dispatcher + recall-path aggregator for skill_improver collect_traces.

Mirrors ops_report/aggregate_pure.py pattern (R-PURE-MODE Wave 3a).
Mode: safe — imports only PURE_STDLIB_ALLOWLIST modules.

If recall produced ≥1 chunk:
  → aggregate chunks inline; return {_path: "recall", ...summary}.
Else:
  → return {_path: "needs_fallback", target_skill, trace_lookback_runs} sentinel.

A downstream unsafe step (collect_traces_fallback in trace_collector.py) then
walks .reyn/events/ on the sentinel and produces the same summary shape.

R-PURE-MODE-REDEFINE wave 4: ``dispatch_traces`` added as the pure hot-path
dispatcher. When recall chunks are present (99% of invocations once
``index_events`` has run), it aggregates inline via
``aggregate_from_recall_chunks_for_traces`` and returns
``{"_path": "recall", ...summary}``. When chunks are absent it returns
``{"_path": "needs_fallback", ...}``, letting a downstream ``mode: unsafe``
step (``collect_traces_fallback``) walk raw events. The ``_path`` sentinel
is stripped before the summary reaches downstream consumers.
"""
from __future__ import annotations

from collections import defaultdict

# ONLY allowlisted stdlib imports — NO glob/os/pathlib

# ── Constants ─────────────────────────────────────────────────────────────────

_ERROR_EXCERPT_MAX = 200
_TOP_ERRORS_MAX = 5


# ── Public API ────────────────────────────────────────────────────────────────


def dispatch_traces(artifact: dict) -> dict:
    """Preprocessor step (mode: safe): dispatch on recall result.

    Receives ``data.trace_recall_result`` (= recall op output: {"chunks": [...]}).
    If chunks present, aggregate them inline via
    ``aggregate_from_recall_chunks_for_traces``. Otherwise emit sentinel for
    the downstream unsafe fallback step.

    Input (from artifact["data"]):
      - ``trace_recall_result``: recall op output or None (on_error: skip)
      - ``skill_name`` / ``target_skill``: target skill being improved
      - ``trace_lookback_runs``: int (default 20)

    Returns either:
      {_path: "recall", skill_name, runs_analyzed, data_source, summary_markdown,
       success_rate, top_errors}
    or:
      {_path: "needs_fallback", target_skill: str, trace_lookback_runs: int}
    """
    data = artifact.get("data") or {}

    recall_result = data.get("trace_recall_result")
    chunks: list[dict] = []
    if isinstance(recall_result, dict):
        raw = recall_result.get("chunks")
        if isinstance(raw, list):
            chunks = raw

    skill_name: str = str(
        data.get("skill_name") or data.get("target_skill") or "unknown"
    )
    lookback: int = int(data.get("trace_lookback_runs") or 20)

    if chunks:
        summary = aggregate_from_recall_chunks_for_traces(
            chunks, target_skill=skill_name, lookback=lookback
        )
        summary["_path"] = "recall"
        return summary

    # No recall chunks — signal the fallback step.
    return {
        "_path": "needs_fallback",
        "target_skill": skill_name,
        "trace_lookback_runs": lookback,
    }


def aggregate_from_recall_chunks_for_traces(
    chunks: list[dict],
    *,
    target_skill: str,
    lookback: int,
) -> dict:
    """Pure aggregation of recall chunks for trace summary.

    Filters chunks by skill_name match (chunks.metadata.extra.skill), caps at
    lookback, then aggregates success_rate / top_errors / version_distribution.

    Returns the same summary shape that collect_traces_fallback returns from
    raw events (minus _path, which the caller adds):
      {
          "skill_name":       str,
          "runs_analyzed":    int,
          "data_source":      "recall" | "empty",
          "summary_markdown": str,
          "success_rate":     float | None,
          "top_errors":       [{"phase": str, "msg": str, "count": int}],
      }
    """
    # Filter to the target skill
    filtered = [
        c for c in chunks
        if str(
            (c.get("metadata") or {}).get("extra", {}).get("skill") or ""
        ) == target_skill
    ]

    # Cap to lookback (recall returns top-k already, but honour the cap)
    filtered = filtered[:lookback]

    if not filtered:
        return _empty_summary(target_skill)

    total = len(filtered)
    success_count = 0
    failure_count = 0
    error_counts: dict[str, int] = defaultdict(int)
    phase_duration: dict[str, list[float]] = defaultdict(list)
    version_hashes: dict[str, dict] = defaultdict(lambda: {"count": 0, "success": 0})

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

        # Phase durations
        phases_visited: list[str] = list(extra.get("phases") or [])
        dur = extra.get("duration_seconds")
        if dur is not None and phases_visited:
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
        skill_name=target_skill,
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
        "skill_name": target_skill,
        "runs_analyzed": total,
        "data_source": "recall",
        "summary_markdown": summary_md,
        "success_rate": success_rate,
        "top_errors": top_errors,
    }


# ── Internal helpers (pure — no I/O) ─────────────────────────────────────────


def _empty_summary(skill_name: str) -> dict:
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
