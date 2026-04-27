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
