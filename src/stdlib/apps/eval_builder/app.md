---
type: app
name: eval_builder
description: Auto-generate an eval spec (eval.md) for an app and run it
entry: analyze_app
final_output: eval_result
final_output_description: |
  Evaluation results after running the generated eval spec against the target app:
  overall score, pass/fail counts, and a summary of results.
finish_criteria:
  - eval.md has been written to the workspace
  - The eval spec has been executed against the target app
  - eval_result captures the score and pass/fail outcome
graph:
  analyze_app: [write_eval]
---

## Overview

Analyzes a target app's DSL, generates a comprehensive `eval.md` spec with
test cases and quality criteria, then immediately runs it.

## Input

```
reyn run eval_builder '{"app_dsl_path": "reyn/local/my_app/app.md", "model": "standard"}'
```

## Output

Evaluation results from running the generated spec. The eval file is written to
`reyn/local/<app_name>/eval.md` (alongside the app's DSL files) and can be re-run manually:

```
reyn eval --spec reyn/local/my_app/eval.md --model standard
```
