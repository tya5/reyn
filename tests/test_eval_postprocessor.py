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

from pathlib import Path

from reyn.stdlib.skills.eval.postprocessor import compute_eval_score

_REPO_ROOT = Path(__file__).resolve().parent.parent


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


# ── Required-string fallbacks (B54 NF-W2-S5 regression) ──────────────────────


def test_failed_target_provides_required_string_fallbacks() -> None:
    """Tier 2: when run_status != "finished" AND LLM omitted the required
    string fields, the function fills in fallbacks so output_schema
    validation (= weakest_phase / summary required as type=string) does
    not fail downstream with "None is not of type 'string'".

    B54 NF-W2-S5 regression — dogfood observed 5/5 eval runs failing at
    postprocessor schema validation because the LLM set weakest_phase=None
    on failed-target runs.
    """
    art = {"data": {"run_status": "failed"}}  # no weakest_phase / summary
    out = compute_eval_score(art)
    assert isinstance(out["weakest_phase"], str) and out["weakest_phase"]
    assert isinstance(out["summary"], str) and out["summary"]


def test_explicit_none_strings_replaced_with_fallback() -> None:
    """Tier 2: LLM explicitly sets weakest_phase=None / summary=None →
    fallback string applied. Otherwise the python step's output_schema
    (required: [..., weakest_phase, summary], both type=string) rejects
    the dict and the eval run is marked failed at the postprocessor
    boundary.
    """
    art = _wrap(
        [_crit(met=True, required=True)],
        weakest_phase=None,
        summary=None,
    )
    out = compute_eval_score(art)
    assert isinstance(out["weakest_phase"], str) and out["weakest_phase"]
    assert isinstance(out["summary"], str) and out["summary"]


def test_vacuous_pass_provides_required_string_fallbacks() -> None:
    """Tier 2: vacuous-pass branch (no required criteria) also provides
    string fallbacks — without the fix, LLMs that omitted these fields
    when the spec had no required criteria would also hit the schema
    failure.
    """
    art = {"data": {"run_status": "finished", "criteria_results": []}}
    out = compute_eval_score(art)
    assert isinstance(out["weakest_phase"], str) and out["weakest_phase"]
    assert isinstance(out["summary"], str) and out["summary"]


# ── spec_path nullable (#1250 regression) ────────────────────────────────────


def test_null_spec_path_validates_against_step_output_schema() -> None:
    """Tier 2: compute_eval_score output carrying spec_path=None passes the
    python step's output_schema validation — the exact #1250 boundary.

    #1250 root cause: a direct-chat eval (target with no spec.md) →
    the LLM emits ``spec_path: null`` in eval_result_raw → compute_eval_score
    passes it through unchanged (it is a pass-through reference field) →
    the python step's output_schema declared ``spec_path: {type: string}``
    so jsonschema rejected the present null with "None is not of type
    'string'" (OutputSchemaViolation), failing the whole eval run at the
    postprocessor even though target + judge finished.

    The fix makes spec_path nullable in the schema (= complete the B49
    W2-S5 nullable migration, which had only relaxed eval_result_raw /
    case_run_result). It is NOT a guard-fill: ``_ensure_required_strings``
    correctly leaves spec_path untouched because null is the meaningful
    "no spec" signal (filling it would fabricate a path).

    Falsification: revert spec_path to ``{type: string}`` in skill.md's
    step output_schema → ``jsonschema.Draft7Validator(...).validate(out)``
    raises here and this test fails.
    """
    import jsonschema
    import yaml

    skill_md = (
        _REPO_ROOT / "src" / "reyn" / "stdlib" / "skills" / "eval" / "skill.md"
    )
    fm = yaml.safe_load(skill_md.read_text(encoding="utf-8").split("---", 2)[1])
    py_step = next(
        s for s in fm["postprocessor"]["steps"] if s["type"] == "python"
    )
    step_schema = py_step["output_schema"]

    out = compute_eval_score(_wrap(
        [_crit(met=True, required=True)],
        spec_path=None,   # direct-chat eval: input carried no spec.md
    ))
    # The pass-through null survives — the guard does NOT fill it.
    assert out["spec_path"] is None
    # The runtime validates the step result with exactly this validator
    # (preprocessor_executor.py: Draft7Validator(step.output_schema)).
    jsonschema.Draft7Validator(step_schema).validate(out)
