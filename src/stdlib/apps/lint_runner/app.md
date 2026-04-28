---
type: app
name: lint_runner
description: Lint a DSL app by name and report errors and warnings
entry: run_lint
final_output: lint_result
final_output_description: Lint results for the target app — pass/fail, error and warning counts, and the list of issues found.
finish_criteria:
  - Linter has been run against the target app
  - Issues have been classified as errors or warnings
  - passed is true only when error_count is 0
---

## Overview

Statically analyzes a DSL app for structural errors and style warnings
without executing any LLM calls.

## Input

```
reyn run lint_runner '{"app": "my_app"}'
```

## Output

`passed: true` means no errors were found (warnings are informational only).
The `issues` list contains every problem with `[ERROR]` or `[WARNING]` prefix.
