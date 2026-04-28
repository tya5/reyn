---
type: phase
name: run_lint
input: lint_request | user_message
role: validator
can_finish: true
---

Run the DSL linter using the `lint` Control IR op and report results.

## Step 1 — Resolve app name

Extract `app` from input. If the input is a user_message, parse the app name from the text.

## Step 2 — Run lint

Emit one lint op:
```json
{"kind": "lint", "app": "{app}"}
```

## Step 3 — Set output fields from the lint result

The lint result contains:
- `passed`: true if error_count == 0
- `error_count` / `warning_count`: issue counts
- `issues`: list of issue strings (empty if none)
- `app`: the app name that was linted

Set output fields:
- `passed`: from lint result
- `error_count` / `warning_count`: from lint result
- `issues`: from lint result
- `app`: from lint result
- `summary`: one sentence — e.g. "No issues found." or "2 errors, 1 warning in article_writer_app."
