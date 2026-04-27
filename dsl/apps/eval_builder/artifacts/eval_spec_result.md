---
type: artifact
name: eval_spec_result
---

# Result of the eval_builder run.

eval_md_path: string
  # Workspace-relative path where eval.md was written.
  # e.g. "eval_specs/writing_review_app/eval.md"

app_dsl_path: string
  # The target app that was analyzed.

case_count: integer
  # Number of test cases generated.

total_criteria: integer
  # Total number of evaluation criteria across all cases and phases.

next_steps: string
  # Instructions for the user: where the file was written, how to run the eval,
  # and (if the target app is in the project dsl/) how to copy it to the right place.
