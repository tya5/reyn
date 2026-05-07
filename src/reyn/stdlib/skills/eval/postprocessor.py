"""Deterministic scoring postprocessor for the eval skill.

Pure-mode python step — runs sandboxed via reyn._python_harness. Reads the
LLM-produced ``eval_result_raw`` artifact (with per-criterion ``criteria_results``,
``run_status``, plus prose fields the LLM authored) and folds in the
deterministic count-and-divide fields the LLM should not be trusted with on
weak models: ``passed_criteria``, ``total_criteria``, ``overall_score``,
``passed``.

The function preserves every field already present in ``data`` (so prose
fields like ``summary``, ``weakest_phase``, ``spec_path`` survive) and adds /
overrides the four scoring fields. The caller-facing artifact (``eval_result``)
does not declare ``additionalProperties: false``, so the pass-through fields
``criteria_results`` and ``run_status`` remain visible to callers that want to
inspect them — they simply aren't part of the documented contract.

Required-criteria semantics: a criterion is **required** when its ``required``
field is ``True`` *or absent* (mirrors the rule documented in evaluate.md).
Optional criteria are excluded from the pass/fail computation.

Edge cases:

- ``run_status`` is set and is not ``"finished"`` — the target skill failed.
  Produce ``passed=False``, ``overall_score=0.0``, counts both 0.
- ``criteria_results`` is empty (or all entries are optional) — vacuous pass:
  ``passed=True``, ``overall_score=1.0``, counts both 0. This matches
  evaluate.md's "Skill finished but no phases evaluated" branch.
- All required criteria met — ``overall_score`` is 1.0 (clean integer
  division) and ``passed`` is True.
- Some required criteria failed — ``overall_score = round(passed/total, 2)``
  and ``passed`` is False (we require *all* required criteria to pass; the
  ``>= 0.6`` floor is preserved purely as an additional guard).
"""


def compute_eval_score(artifact: dict) -> dict:
    """Return a new ``data`` dict with deterministic scoring fields merged in.

    The caller writes the return value back at ``into: data``, so the dict
    must include every field the caller-facing schema requires plus any
    pass-through fields the LLM authored.
    """
    data = dict(artifact.get("data", {}) or {})
    criteria = data.get("criteria_results", []) or []
    run_status = data.get("run_status", "") or ""

    # ── Edge: target skill did not finish ────────────────────────────────
    if run_status and run_status != "finished":
        data["passed"] = False
        data["overall_score"] = 0.0
        data["passed_criteria"] = 0
        data["total_criteria"] = 0
        return data

    # Required iff `required` is True or absent.
    required_items = [
        c for c in criteria
        if c.get("required", True) is not False
    ]
    total = len(required_items)
    passed_count = sum(
        1 for c in required_items if c.get("met") is True
    )

    if total == 0:
        # Vacuous pass — no required criteria to evaluate.
        data["passed"] = True
        data["overall_score"] = 1.0
        data["passed_criteria"] = 0
        data["total_criteria"] = 0
        return data

    overall = round(passed_count / total, 2)
    all_required_met = passed_count == total

    data["passed_criteria"] = passed_count
    data["total_criteria"] = total
    data["overall_score"] = overall
    # Strict: `passed` is True only when every required criterion is met.
    # The ``overall_score >= 0.6`` floor in the original LLM instructions is
    # subsumed here (all_required_met implies overall_score == 1.0), but we
    # keep both clauses to make the intent explicit.
    data["passed"] = all_required_met and overall >= 0.6
    return data
