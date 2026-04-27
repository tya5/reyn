---
type: app
name: app_builder
description: Generate a new app from a natural language description
entry: plan_app
final_output: lint_result
final_output_description: |
  Lint results for the generated app: pass/fail status, error and warning counts,
  and the list of any issues found in the generated DSL files.
finish_criteria:
  - All DSL files for the app have been written to the workspace
  - The generated DSL has been linted and lint_result has been produced
  - passed is true only when no lint errors were found
---

plan_app -> build_app
build_app -> @lint_runner[shared]
