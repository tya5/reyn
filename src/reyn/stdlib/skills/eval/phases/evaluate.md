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

Aggregate the per-phase judgments (in `data.judgments`) into a single `eval_result_raw` artifact. The deterministic scoring fields (`passed_criteria`, `total_criteria`, `overall_score`, `passed`) are **not** part of your output — they are computed by the skill's postprocessor from `criteria_results` after you finish. Focus only on flattening the judgments and authoring the prose fields.

`data.judgments` is a list of `run_skill` op results — one per successfully judged phase. The actual `phase_judgment` payload for entry `i` lives at `data.judgments[i].final_output` and contains: `phase_name`, `passed`, `score`, `criteria_results`, `summary`.

Throughout this phase, when a step refers to a "judgment" field (e.g. `phase_name`, `score`, `criteria_results`), look at the `final_output` of each entry in `data.judgments` — never the wrapping op result fields (`kind`, `status`, `success`, etc.).

## Required-criteria semantics

A criterion in `final_output.criteria_results` is **required** when its `required` field is `true` OR the `required` field is absent. Optional (aspirational) criteria are those where `required` is explicitly `false`. Preserve each criterion's `required` value verbatim in your output — do not rewrite or normalize it.

## Build the output

- `criteria_results`: a flat array assembled by walking each `data.judgments[i].final_output` (call it `j`) and emitting one entry per criterion in `j.criteria_results`. Each emitted entry has `phase_name = j.phase_name` plus `description`, `required`, `met`, `reason` copied verbatim from the source criterion. If `required` is absent on the source, set it to `true` in the output.
- `weakest_phase`: `final_output.phase_name` of the judgment with the lowest `final_output.score`. Empty string when there are no judgments.
- `spec_path`: from `data.spec_path`.
- `run_status`: from `data.run_status`. Copy verbatim — the postprocessor reads this to detect failed target runs.
- `summary`: 2–3 sentences describing what passed, what failed, and the most significant issue. If any optional criteria failed, mention them as aspirational shortfalls.

## Edge cases

**Skill run did not finish** (`data.run_status != "finished"`): emit `criteria_results: []`, `weakest_phase: ""`, copy `run_status` verbatim, and note in the summary that the target skill run failed with the given status. The postprocessor will set `passed=false` and zero counts.

**Skill finished but no phases evaluated** (`run_status == "finished"` and `data.eval_requests` was empty or all items were skipped): emit `criteria_results: []`, `weakest_phase: ""`, `run_status: "finished"`, and note in the summary that no phases were evaluated. The postprocessor will treat the empty list as a vacuous pass.
