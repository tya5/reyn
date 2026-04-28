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
graph:
  plan_app: [design_artifacts]
  design_artifacts: [review_plan]
  review_plan: [build_app]
  build_app: ["@lint_runner[shared]"]
---

## Overview

Describe an app in natural language and app_builder will generate the full DSL file set
(app.md, phases, artifacts) and run the linter to verify the output.

## Input

Provide a natural language description of the app you want to build.

```
reyn run app_builder "Build an app that writes an article and reviews it before delivery"
```

Alternatively, pass a structured `app_request` artifact with `app_name`, `description`, and `goal`.

## Output

Lint results for the generated app. When `passed: true`, the app is ready to run:

```
reyn run <app_name> "<your input>"
```
