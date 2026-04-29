---
type: phase
name: run_and_eval
input: improvement_session
role: evaluator
model_class: standard
---

Run the target app against the chosen test case via the `eval` stdlib sub-app, capture the score for this iteration, and update the workspace history file.

This phase is re-entered each iteration of the improvement loop (via rollback chain from apply_improvements).

## Step 1 — Read the iteration history

Issue a file read for `improver_state.json` in the workspace root.

The file contains:
```json
{"session": {...}, "iterations": [...]}
```

The current iteration index is `len(iterations) + 1`.
The `iterations` array represents only iterations whose `apply_improvements` has finished — never includes the in-flight iteration.

## Step 2 — Build eval_case_input

Construct the input artifact for the `eval` sub-app from the input `improvement_session`:

```json
{
  "type": "eval_case_input",
  "data": {
    "case_name": "<session.case_name>",
    "case_input": "<session.case_input>",
    "spec_path": "<session.eval_spec_path>",
    "target_skill_path": "<session.target_skill_path>",
    "dsl_root": "<session.target_dsl_root>",
    "phase_criteria": <session.phase_criteria>
  }
}
```

Pass `phase_criteria` through verbatim.

## Step 3 — Invoke eval (ONCE)

Issue a `run_skill` Control IR op:

```
{
  "kind": "run_skill",
  "app": "eval",
  "input": <the eval_case_input from Step 2>,
  "model": "<session.model>",
  "workspace": "isolated"
}
```

CRITICAL: Run eval EXACTLY ONCE per visit to this phase. After the first `run_skill [finished]` result arrives, proceed to Step 4 — DO NOT issue a second `run_skill` op even if you doubt the result. Repeating the eval wastes tokens without producing different output.

If the sub-app aborts (status != "finished"), do NOT abort the improver — produce an iteration_state with `latest_eval.passed = false`, `latest_eval.overall_score = 0.0`, `latest_eval.weakest_phase = "<unknown — eval aborted>"`, and `latest_eval.summary` describing the failure. The improver loop will treat this as a low score and either continue trying or terminate via the regression/stagnation rules.

## Step 4 — Build iteration_state

From the run_skill result's final_output (the `eval_result` data), construct `iteration_state`:

```json
{
  "session": <the input improvement_session, unchanged>,
  "current_iteration": <len(iterations) + 1>,
  "latest_eval": {
    "passed":          <eval_result.passed>,
    "overall_score":   <eval_result.overall_score>,
    "passed_criteria": <eval_result.passed_criteria>,
    "total_criteria":  <eval_result.total_criteria>,
    "weakest_phase":   <eval_result.weakest_phase>,
    "summary":         <eval_result.summary>
  },
  "history": <iterations array from improver_state.json, unchanged>
}
```

Do NOT modify `improver_state.json` here. apply_improvements is responsible for committing the new iteration into history (because the iteration is only complete after changes have been applied).

## Output

Emit `iteration_state` and choose `transition` → `plan_improvements`.
