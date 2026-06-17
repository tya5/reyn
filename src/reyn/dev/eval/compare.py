"""reyn.dev.eval.compare — pure-function regression diff logic (FP-0007 Component C).

``compute_diff(baseline_result, candidate_result, threshold)`` compares two
eval run result dicts (each produced by ``load_eval_result``) and returns a
structured diff dict suitable for text or JSON rendering.

P7 compliant: no skill-specific strings.  All field names here are eval-infra
OS-level concepts (score, case_id, run_id, skill_version_hash).
"""
from __future__ import annotations

from typing import Any


def compute_diff(
    baseline_result: dict,
    candidate_result: dict,
    threshold: float = 0.05,
) -> dict:
    """Compute a regression diff between two eval run results.

    Parameters
    ----------
    baseline_result:
        Dict with keys ``run_id``, ``skill_version_hash``, ``timestamp``,
        and ``cases`` (list of case dicts with ``case_id`` and ``score``).
    candidate_result:
        Same structure as baseline_result.
    threshold:
        Score drop magnitude that triggers a regression alert.
        A case is "regressing" when ``candidate.score - baseline.score < -threshold``.

    Returns
    -------
    dict with keys:
        baseline         — {run_id, skill_version_hash, timestamp}
        candidate        — {run_id, skill_version_hash, timestamp}
        threshold        — float
        warning          — str or None (e.g. "identical skill version")
        summary          — {cases_compared, mean_delta, max_regression,
                            max_improvement, regressing_count}
        regressing_cases — list of {case_id, baseline_score, candidate_score, delta}
        missing_in_candidate — list of case_id strings
        missing_in_baseline  — list of case_id strings
        alert            — bool (True when regressing_count > 0)
    """
    b_meta = _meta(baseline_result)
    c_meta = _meta(candidate_result)

    # Build lookup maps: case_id → score
    b_map: dict[str, float] = {
        c["case_id"]: float(c["score"])
        for c in baseline_result.get("cases", [])
        if "case_id" in c
    }
    c_map: dict[str, float] = {
        c["case_id"]: float(c["score"])
        for c in candidate_result.get("cases", [])
        if "case_id" in c
    }

    missing_in_candidate = sorted(set(b_map) - set(c_map))
    missing_in_baseline = sorted(set(c_map) - set(b_map))
    common = sorted(set(b_map) & set(c_map))

    # Per-case deltas
    deltas: list[dict[str, Any]] = []
    for cid in common:
        b_score = b_map[cid]
        c_score = c_map[cid]
        delta = round(c_score - b_score, 6)
        deltas.append(
            {
                "case_id": cid,
                "baseline_score": b_score,
                "candidate_score": c_score,
                "delta": delta,
            }
        )

    # Aggregate
    regressing = [d for d in deltas if d["delta"] < -threshold]
    regressing_sorted = sorted(regressing, key=lambda d: d["delta"])

    mean_delta: float | None = None
    max_regression: dict | None = None
    max_improvement: dict | None = None

    if deltas:
        mean_delta = round(sum(d["delta"] for d in deltas) / len(deltas), 6)

        worst = min(deltas, key=lambda d: d["delta"])
        if worst["delta"] < 0:
            max_regression = {"case_id": worst["case_id"], "delta": worst["delta"]}

        best = max(deltas, key=lambda d: d["delta"])
        if best["delta"] > 0:
            max_improvement = {"case_id": best["case_id"], "delta": best["delta"]}

    # Warning: identical version hashes
    warning: str | None = None
    b_hash = b_meta.get("skill_version_hash")
    c_hash = c_meta.get("skill_version_hash")
    if b_hash and c_hash and b_hash == c_hash:
        warning = "identical skill version"

    alert = len(regressing) > 0

    return {
        "baseline": b_meta,
        "candidate": c_meta,
        "threshold": threshold,
        "warning": warning,
        "summary": {
            "cases_compared": len(common),
            "mean_delta": mean_delta,
            "max_regression": max_regression,
            "max_improvement": max_improvement,
            "regressing_count": len(regressing),
        },
        "regressing_cases": regressing_sorted,
        "missing_in_candidate": missing_in_candidate,
        "missing_in_baseline": missing_in_baseline,
        "alert": alert,
    }


def _meta(result: dict) -> dict:
    """Extract run metadata from a result dict."""
    return {
        "run_id": result.get("run_id", "unknown"),
        "skill_version_hash": result.get("skill_version_hash") or "unknown",
        "timestamp": result.get("timestamp", "unknown"),
    }
