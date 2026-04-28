---
type: app
name: app_improver
description: Run, analyze, and automatically improve an existing app
entry: prepare
final_output: improvement_result
final_output_description: |
  Summary of improvements applied to the target app: which files were modified,
  what was changed, and a suggestion for what to verify next.
finish_criteria:
  - All planned DSL file improvements have been written to disk
  - The improvement summary describes concrete changes made
  - Next verification steps are specified
graph:
  prepare: [run_target]
  run_target: [analyze_execution]
  analyze_execution: [plan_improvements]
  plan_improvements: [apply_improvements]
---

## Overview

Runs a target app with a test input, analyzes the execution trace (phase quality,
retry counts, artifact content), and automatically rewrites DSL files to address
the identified issues.

## Input

```
reyn run app_improver '{
  "app_dsl_path": "reyn/project/my_app/app.md",
  "dsl_root": "reyn/project/my_app",
  "test_input": "your test input here",
  "model": "standard",
  "improvement_focus": "instruction clarity"
}'
```

`improvement_focus` is optional but helps direct the analysis toward a specific
concern (e.g. `"artifact schema completeness"`, `"review phase quality"`).

## Output

List of modified files and a prose summary of changes. Re-run the app after
improvement to verify the results.
