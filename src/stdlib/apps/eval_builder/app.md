---
type: app
name: eval_builder
entry: analyze_app
final_output: eval_spec_result
final_output_description: |
  Path to the generated eval.md file, plus a summary of how many cases
  and criteria were created, and where to copy the file.
finish_criteria:
  - eval.md has been written to the workspace
  - At least one test case with phase-level and final criteria exists
  - next_steps tells the user where the file was written
---

analyze_app -> write_eval
