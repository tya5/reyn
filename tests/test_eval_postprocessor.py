"""Tier 2: eval skill postprocessor pins arithmetic correctness.

Pure function unit tests for `compute_eval_score` — the python step that
runs after the LLM finishes the eval skill. The function takes the LLM's
`eval_result_raw` artifact (with per-criterion `criteria_results` plus
`run_status`) and folds in deterministic scoring fields. We pin:

* counts: `passed_criteria`, `total_criteria` exactly match required-criterion
  counts (optional criteria excluded);
* `overall_score`: `passed/total` rounded to 2 decimals, with documented
  edge values for empty / failed runs;
* `passed`: True iff every required criterion is met AND the score floor
  (>= 0.6) is satisfied;
* edge cases: empty `criteria_results`, `run_status != "finished"`, missing
  `required` field, and explicit `required: false` exclusion.

No mocks, no LLM, no I/O — a direct import of the pure function. Public
contract only: the function's input dict shape and return dict shape.
"""
from __future__ import annotations

from reyn.stdlib.skills.eval.postprocessor import compute_eval_score


def _wrap(criteria, *, run_status: str = "finished", **extra) -> dict:
    """Build a minimal `eval_result_raw` artifact dict for the function."""
    data = {
        "criteria_results": criteria,
        "run_status": run_status,
        "weakest_phase": extra.pop("weakest_phase", "p1"),
        "spec_path": extra.pop("spec_path", "spec.md"),
        "summary": extra.pop("summary", "summary text"),
    }
    data.update(extra)
    return {"type": "eval_result_raw", "data": data}


def _crit(*, met: bool, required: bool | None = None,
          phase: str = "p1", desc: str = "c", reason: str = "r") -> dict:
    out = {
        "phase_name": phase, "description": desc,
        "met": met, "reason": reason,
    }
    if required is not None:
        out["required"] = required
    return out


# ── Happy paths: passed/total/score arithmetic ───────────────────────────────


def test_all_required_criteria_passed() -> None:
    """Tier 2: 3/3 required passed → overall_score=1.0, passed=True."""
    art = _wrap([
        _crit(met=True, required=True),
        _crit(met=True, required=True),
        _crit(met=True, required=True),
    ])
    out = compute_eval_score(art)
    assert out["passed_criteria"] == 3
    assert out["total_criteria"] == 3
    assert out["overall_score"] == 1.0
    assert out["passed"] is True


def test_partial_pass_two_thirds() -> None:
    """Tier 2: 2/3 required passed → overall_score=0.67, passed=False."""
    art = _wrap([
        _crit(met=True, required=True),
        _crit(met=True, required=True),
        _crit(met=False, required=True),
    ])
    out = compute_eval_score(art)
    assert out["passed_criteria"] == 2
    assert out["total_criteria"] == 3
    assert out["overall_score"] == 0.67
    assert out["passed"] is False


def test_one_third_passed() -> None:
    """Tier 2: 1/3 required passed → overall_score=0.33, passed=False."""
    art = _wrap([
        _crit(met=True, required=True),
        _crit(met=False, required=True),
        _crit(met=False, required=True),
    ])
    out = compute_eval_score(art)
    assert out["passed_criteria"] == 1
    assert out["total_criteria"] == 3
    assert out["overall_score"] == 0.33
    assert out["passed"] is False


def test_all_required_failed() -> None:
    """Tier 2: 0/3 required passed → overall_score=0.0, passed=False."""
    art = _wrap([
        _crit(met=False, required=True),
        _crit(met=False, required=True),
        _crit(met=False, required=True),
    ])
    out = compute_eval_score(art)
    assert out["passed_criteria"] == 0
    assert out["total_criteria"] == 3
    assert out["overall_score"] == 0.0
    assert out["passed"] is False


# ── Required-criteria semantics ──────────────────────────────────────────────


def test_missing_required_field_treated_as_required() -> None:
    """Tier 2: criterion without explicit `required` is required (per spec)."""
    art = _wrap([
        _crit(met=True),                # no required field — counts
        _crit(met=False, required=True),
    ])
    out = compute_eval_score(art)
    assert out["total_criteria"] == 2
    assert out["passed_criteria"] == 1
    assert out["passed"] is False


def test_optional_criteria_excluded_from_counts() -> None:
    """Tier 2: `required: false` criteria do not count toward total/passed."""
    art = _wrap([
        _crit(met=True, required=True),
        _crit(met=False, required=False),  # optional fail — ignored
        _crit(met=True, required=False),   # optional pass — also ignored
    ])
    out = compute_eval_score(art)
    assert out["total_criteria"] == 1
    assert out["passed_criteria"] == 1
    assert out["overall_score"] == 1.0
    assert out["passed"] is True


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_criteria_is_vacuous_pass() -> None:
    """Tier 2: no required criteria → passed=True, score=1.0, counts=0."""
    art = _wrap([])
    out = compute_eval_score(art)
    assert out["total_criteria"] == 0
    assert out["passed_criteria"] == 0
    assert out["overall_score"] == 1.0
    assert out["passed"] is True


def test_run_status_not_finished_zeroes_everything() -> None:
    """Tier 2: target skill failed → passed=False, score=0.0, counts=0."""
    art = _wrap(
        [_crit(met=True, required=True)],   # would otherwise pass
        run_status="aborted",
    )
    out = compute_eval_score(art)
    assert out["passed"] is False
    assert out["overall_score"] == 0.0
    assert out["passed_criteria"] == 0
    assert out["total_criteria"] == 0


def test_only_optional_criteria_is_vacuous_pass() -> None:
    """Tier 2: all optional → no required to evaluate → vacuous pass."""
    art = _wrap([
        _crit(met=False, required=False),
        _crit(met=False, required=False),
    ])
    out = compute_eval_score(art)
    assert out["total_criteria"] == 0
    assert out["passed_criteria"] == 0
    assert out["overall_score"] == 1.0
    assert out["passed"] is True


# ── Field preservation (LLM-authored prose survives) ─────────────────────────


def test_preserves_llm_authored_fields() -> None:
    """Tier 2: weakest_phase / spec_path / summary pass through unchanged."""
    art = _wrap(
        [_crit(met=True, required=True)],
        weakest_phase="generate",
        spec_path="reyn/local/x/eval.md",
        summary="All good.",
    )
    out = compute_eval_score(art)
    assert out["weakest_phase"] == "generate"
    assert out["spec_path"] == "reyn/local/x/eval.md"
    assert out["summary"] == "All good."
    # Pass-through fields the caller-facing schema does not require also
    # remain visible (no whitelist filtering in the function).
    assert out["run_status"] == "finished"
    assert "criteria_results" in out
