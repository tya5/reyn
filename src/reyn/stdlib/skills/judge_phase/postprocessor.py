"""Deterministic score computation for the judge_phase skill.

The LLM produces a `phase_judgment_raw` artifact (= per-criterion `met`
booleans, overall `passed`, prose `summary`). LLMs are unreliable at
arithmetic, so the `score` field — = passed_count / total_count of
criteria_results — is computed in pure Python here and merged into the
caller-facing `phase_judgment` artifact by the postprocessor pipeline.

This module is invoked via the `python` postprocessor step declared in
`skill.md`; the function receives the wrapped artifact `{type, data}` and
returns a number to be placed at `data.score`.
"""
from __future__ import annotations

from typing import Any


def compute_score(artifact: dict[str, Any]) -> float:
    """Return passed/total over criteria_results, rounded to two decimals.

    Args:
        artifact: Wrapped finish artifact `{type, data}` where `data`
            contains `criteria_results` — a list of `{met: bool, ...}`
            entries.

    Returns:
        Float in [0.0, 1.0] rounded to 2 decimals. 0.0 when
        `criteria_results` is missing or empty.
    """
    data = artifact.get("data", {}) or {}
    results = data.get("criteria_results", []) or []
    total = len(results)
    if total == 0:
        return 0.0
    passed = sum(1 for r in results if r.get("met") is True)
    return round(passed / total, 2)
