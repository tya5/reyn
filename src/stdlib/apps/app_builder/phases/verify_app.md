---
type: phase
name: verify_app
input: build_result
role: dsl_verifier
can_finish: true
---

Run the lint op against the app that was just written, then handle the result.

```
{"kind": "lint", "app_path": "<data.app_path>"}
```

If lint returns `passed: false`:
- Emit `control.type="rollback"` with a reason that lists the lint issues verbatim — the OS will re-run build_app with your feedback so it can fix the files using the original app_plan
- Do NOT attempt to fix files yourself — you do not have the app_plan context needed to regenerate correct content
- Do NOT finish if lint has errors

If lint passes, finish with an `app_builder_result` artifact:
- `app_name`: from data.app_name
- `app_path`: from data.app_path
- `files_written`: from data.files_written
- `file_count`: from data.file_count
- `lint_passed`: true
- `lint_issues`: []
- `summary`: one sentence describing what the app does for its users

summary MUST describe what the app does for its users — not what you (the builder) did.
Good: "An app that lets users submit documents for reviewer approval or rejection with reasons."
Bad: "Generated DSL files for the review app and saved them to the workspace."
