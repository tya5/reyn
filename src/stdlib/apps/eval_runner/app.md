---
type: app
name: eval_runner
description: Run an eval spec and report pass/fail scores per phase
entry: run_eval
final_output: eval_result
final_output_description: Evaluation results — overall score, pass/fail counts, and a summary of which cases passed or failed.
finish_criteria:
  - Eval has been run against the target spec
  - Overall score and pass/fail counts are recorded
  - passed is true only when overall_score >= 0.6
---

## Overview

Runs an `eval.md` specification against a target app across multiple test cases
and reports per-phase pass rates and an overall score.

## Input

```
reyn run eval_runner '{"spec_path": "eval_specs/my_app/eval.md", "model": "standard"}'
```

`spec_path` is relative to the workspace root.
`model` accepts a LiteLLM model name or a class alias (`standard`, `light`, `strong`).

## Output

`passed: true` when `overall_score >= 0.6`. The `weakest_phase` field identifies
where the app most needs improvement.
