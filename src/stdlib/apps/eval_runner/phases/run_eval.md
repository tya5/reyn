---
type: phase
name: run_eval
input: eval_request | user_message
input_description: |
  Either a structured eval_request (spec_path: workspace-relative path to eval.md,
  model: LiteLLM model string, app_name: optional hint) or a user_message describing what to evaluate.
  If spec_path or model is missing, use ask_user.
role: validator
can_finish: true
---

Run the eval spec using the `eval` Control IR op and report results.

## Step 1 — Resolve inputs

Extract from input:
- `spec_path`: workspace-relative path to the eval.md file (e.g. `"eval_specs/my_app/eval.md"`)
- `model`: LiteLLM model string to use for running the target app
- `judge_model`: optional; defaults to `model` if omitted

## Step 2 — Run eval

Emit one eval op:
```json
{"kind": "eval", "spec_path": "{spec_path}", "model": "{model}"}
```

## Step 3 — Set output fields from the eval result

The eval result contains:
- `passed`: true if overall_score >= 0.6
- `overall_score`: float 0.0–1.0
- `passed_criteria` / `total_criteria`: counts
- `weakest_phase`: name of the lowest-scoring phase, or empty string
- `case_count`: number of test cases run
- `cases`: per-case breakdown (name, score, passed, total)

Set output fields:
- `passed`: from eval result
- `overall_score`: from eval result
- `passed_criteria` / `total_criteria`: from eval result
- `weakest_phase`: from eval result
- `spec_path`: the path that was evaluated
- `summary`: one sentence — e.g. "All 12 criteria passed (score 1.00)." or "6/12 criteria passed (score 0.50) — weakest: analyze."
