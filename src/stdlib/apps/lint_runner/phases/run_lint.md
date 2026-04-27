---
type: phase
name: run_lint
input: lint_request | user_message
input_description: |
  Either a structured lint_request (dsl_root: CWD-relative or workspace-relative path to lint,
  app_name: optional hint for path resolution) or a user_message with a natural language description
  of what to lint. If dsl_root is missing, use ask_user.
role: validator
can_finish: true
---

Run `agent-os lint` against the target DSL directory and report issues.

## Step 1 — Resolve dsl_root

Extract `dsl_root` from input (e.g. `"dsl"`, `"dsl/"`, `"workspace/my_run/dsl"`).

Try to run the lint command directly:
```
agent-os lint --dsl {dsl_root} 2>&1; echo "EXIT:$?"
```

If the command fails with "No such file or directory" or "does not exist", the path is likely
workspace-relative. Locate the actual directory with:
```
find . -type d -name "apps" -path "*/dsl/apps" -not -path "*/src/*" -not -path "*/__pycache__/*" 2>/dev/null | sed 's|/apps$||' | head -5
```
Pick the result that contains the app (use app_name hint if available). Re-run lint with the resolved path.

## Step 2 — Parse output

`agent-os lint` outputs one line per issue:
```
[ERROR  ] path/to/file.md  →  message
[WARNING] path/to/file.md  →  message
```
Or: `No issues found.`

Count errors and warnings. Collect each issue line as a string in `issues`.

## Step 3 — Set output fields

- `passed`: true if error_count == 0 (warnings do not fail)
- `error_count` / `warning_count`: counts from parsed output
- `issues`: list of raw issue lines (empty array if none)
- `dsl_root`: the resolved path actually linted
- `summary`: one sentence — e.g. "No issues found." or "2 errors, 1 warning found in dsl/apps/my_app."
