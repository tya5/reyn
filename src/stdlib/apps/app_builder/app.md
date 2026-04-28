---
type: app
name: app_builder
description: Generate a new app from a natural language description
entry: plan_app
final_output: app_builder_result
final_output_description: |
  Build result for the generated app: name, path, files written, and lint outcome.
finish_criteria:
  - All DSL files for the app have been written to the workspace
  - The generated DSL has been linted with no errors
graph:
  plan_app: [design_artifacts]
  design_artifacts: [review_plan]
  review_plan: [build_app]
---

## Overview

Describe an app in natural language and app_builder will generate the full DSL file set
(app.md, phases, artifacts) and verify the output with the linter.

## Input

Provide a natural language description of the app you want to build.

```
reyn run app_builder "Build an app that writes an article and reviews it before delivery"
```

Alternatively, pass a structured `app_request` artifact with `app_name`, `description`, and `goal`.

## Output

Build result for the generated app. When `lint_passed: true`, the app is ready to run:

```
reyn run <app_name> "<your input>"
```
