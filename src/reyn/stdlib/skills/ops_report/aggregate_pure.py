"""Pure (mode: safe) aggregation helpers for ops_report.

Split out of ``aggregate.py`` so that ``aggregate_from_recall_chunks`` can be
declared ``mode: safe`` — the AST validator rejects the whole-module load of
``aggregate.py`` because of its ``import glob`` / ``import os`` at module top.
This sibling module imports only PURE_STDLIB_ALLOWLIST entries, so
``mode: safe`` (= "ambient sources only" contract) holds at parse time.

The split is purely about syntactic reachability of unsafe modules:
``aggregate_from_recall_chunks`` itself was already pure; the only blocker
was that it lived alongside I/O-using siblings.

Private helpers ``_ERROR_SAMPLE_MAX``, ``_ERROR_EXCERPT_MAX``, and
``_top_failing_skills`` are duplicated here (not imported from ``aggregate.py``)
so that this module's import graph stays clean and the AST validator sees only
PURE_STDLIB_ALLOWLIST imports. The duplication is intentional: coupling to
``aggregate.py`` would re-introduce the unsafe-import reachability and defeat
the point of the split. Both copies are small; keep them in sync by hand if
they ever diverge.

R-PURE-MODE-REDEFINE wave 2; see
docs/deep-dives/audits/2026-05-15-pure-mode-stdlib-audit.md.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any  # noqa: F401

# ── Constants (duplicated from aggregate.py — intentional, see module docstring) ─

_ERROR_SAMPLE_MAX = 5
_ERROR_EXCERPT_MAX = 200


# ── Public API ────────────────────────────────────────────────────────────────


def aggregate_from_recall_chunks(chunks: list[dict]) -> dict:
    """Aggregate from recall chunks (already filtered by recall query).

    Each chunk has the shape returned by the events source:
        {
            "content":  str,
            "metadata": {
                "extra": {
                    "skill":            str,
                    "status":           str,          # "success"|"failed"|"aborted"
                    "duration_seconds": int | None,
                    "errors":           list[str],
                    "started_at":       str,
                    "completed_at":     str,
                    ...
                }
            }
        }

    Returns the same shape as aggregate_from_raw_events.
    Period information is not available from chunks alone — period_days is
    set to None in the returned dict to signal this.
    """
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

    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        extra = meta.get("extra") or {}

        skill_name = str(extra.get("skill") or "unknown")
        status = str(extra.get("status") or "unknown").lower()
        duration = extra.get("duration_seconds")
        errors = list(extra.get("errors") or [])

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
            try:
                sk["_duration_sum"] += float(duration)
                sk["_duration_count"] += 1
            except (TypeError, ValueError):
                pass

        for err in errors:
            if len(errors_sample) < _ERROR_SAMPLE_MAX:
                errors_sample.append(str(err)[:_ERROR_EXCERPT_MAX])

    # Compute per-skill avg_duration
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
        "period_days": None,
        "by_skill": by_skill_clean,
        "top_failing_skills": top_failing,
        "errors_sample": errors_sample,
    }


# ── Internal helpers (duplicated from aggregate.py — intentional, see module docstring) ─


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
