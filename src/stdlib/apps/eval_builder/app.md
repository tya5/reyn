---
type: app
name: eval_builder
description: Auto-generate an eval spec (eval.md) for an app
entry: analyze_app
final_output: eval_result
final_output_description: |
  Evaluation results after running the generated eval spec against the target app:
  overall score, pass/fail counts, and a summary of results.
finish_criteria:
  - eval.md has been written to the workspace
  - The eval spec has been executed against the target app
  - eval_result captures the score and pass/fail outcome
---

analyze_app -> write_eval
write_eval -> @eval_runner[shared]
