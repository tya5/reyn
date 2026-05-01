---
type: phase
name: evaluate
input: case_run_result
role: evaluator
can_finish: true
allowed_ops: []
preprocessor:
  - type: iterate
    over: data.eval_requests
    apply:
      type: run_op
      op:
        kind: run_skill
        skill: judge_phase
        input: {}
      args_from:
        input: "_iter.item"
    into: data.judgments
    on_error: skip
---

Aggregate the per-phase judgments (in `data.judgments`) into a single `eval_result`.

`data.judgments` is a list of `run_skill` op results — one per successfully judged phase. The actual `phase_judgment` payload for entry `i` lives at `data.judgments[i].final_output` and contains: `phase_name`, `passed`, `score`, `criteria_results`, `summary`.

Throughout this phase, when a step refers to a "judgment" field (e.g.
`phase_name`, `passed`, `score`, `criteria_results`), look at the
`final_output` of each entry in `data.judgments` — never the wrapping op
result fields (`kind`, `status`, `success`, etc.).

## Required-criteria semantics

A criterion in `final_output.criteria_results` is **required** when its `required` field is `true` OR the `required` field is absent. Optional (aspirational) criteria are those where `required` is explicitly `false` — they are reported in the summary but excluded from the pass/fail computation.

## Compute

- `total_criteria`: number of **required** criteria across all `judgments[i].final_output`
- `passed_criteria`: number of **required** criteria where `met=true` across all `judgments[i].final_output`
- `overall_score`: `passed_criteria / total_criteria` (use 1.0 if `total_criteria` is 0)
- `passed`: true only if `overall_score >= 0.6` AND every required criterion across all judgments has `met=true`
- `weakest_phase`: `final_output.phase_name` of the judgment with the lowest `final_output.score` (empty string if no judgments)
- `spec_path`: from `data.spec_path`
- `summary`: 2–3 sentences describing what passed, what failed, and the most significant issue. If any optional criteria failed, mention them as aspirational shortfalls.

## Edge cases

**Skill run did not finish** (`data.run_status != "finished"`): produce `passed=false`, `overall_score=0.0`, `passed_criteria=0`, `total_criteria=0`, `weakest_phase=""`, and note in the summary that the target skill run failed with the given status.

**Skill finished but no phases evaluated** (`run_status == "finished"` and `data.eval_requests` was empty or all items were skipped): produce `passed=true`, `overall_score=1.0`, `passed_criteria=0`, `total_criteria=0`, `weakest_phase=""`, and note in the summary that no phases were evaluated.
