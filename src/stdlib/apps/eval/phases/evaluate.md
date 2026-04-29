---
type: phase
name: evaluate
input: case_run_result
role: evaluator
can_finish: true
preprocessor:
  - type: iterate
    over: data.eval_requests
    apply:
      type: run_app
      app: judge_phase
    into: data.judgments
    on_error: skip
---

Aggregate the per-phase judgments (in `data.judgments`) into a single `eval_result`.

`data.judgments` is a list of `phase_judgment` objects — one per successfully judged phase. Each has: `phase_name`, `passed`, `score`, `criteria_results`, `summary`.

Compute:
- `passed_criteria`: total count of criteria where `met=true` across all judgments
- `total_criteria`: total count of criteria across all judgments
- `overall_score`: `passed_criteria / total_criteria` (1.0 if total_criteria is 0)
- `passed`: true only if every judgment has `passed=true`
- `weakest_phase`: `phase_name` of the judgment with the lowest `score` (empty string if no judgments)
- `spec_path`: from `data.spec_path`
- `summary`: 2–3 sentences describing what passed, what failed, and the most significant issue

If `data.eval_requests` was empty or all items were skipped, produce `passed=true`, `overall_score=1.0`, `passed_criteria=0`, `total_criteria=0`, `weakest_phase=""`, and note in the summary that no phases were evaluated.
