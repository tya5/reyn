---
type: app
name: lint_runner
entry: run_lint
final_output: lint_result
final_output_description: Lint results for the target DSL directory — pass/fail, error and warning counts, and the list of issues found.
finish_criteria:
  - Linter has been run against the target DSL directory
  - Issues have been classified as errors or warnings
  - passed is true only when error_count is 0
---
