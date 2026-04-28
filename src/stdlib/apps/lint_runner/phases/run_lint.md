---
type: phase
name: run_lint
input: lint_request | user_message
role: validator
can_finish: true
---

Run the DSL linter using the `lint` Control IR op and report results.

## Step 1 — Resolve dsl_root

Extract `dsl_root` from input. Default to `"reyn/"` if not specified.

## Step 2 — Run lint

Emit one lint op:
```json
{"kind": "lint", "dsl_root": "{dsl_root}"}
```

## Step 3 — Set output fields from the lint result

The lint result contains:
- `passed`: true if error_count == 0
- `error_count` / `warning_count`: issue counts
- `issues`: list of issue strings (empty if none)
- `dsl_root`: the path that was linted

Set output fields:
- `passed`: from lint result
- `error_count` / `warning_count`: from lint result
- `issues`: from lint result
- `dsl_root`: from lint result
- `summary`: one sentence — e.g. "No issues found." or "2 errors, 1 warning in dsl/."
