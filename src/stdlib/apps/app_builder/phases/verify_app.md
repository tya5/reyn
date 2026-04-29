---
type: phase
name: verify_app
input: build_result
role: dsl_verifier
can_finish: true
max_act_turns: 1
---

## Step 1 — Run lint (your ONLY act turn)

Issue exactly one lint op:

```
{"kind": "lint", "app_path": "<data.app_path>"}
```

## Step 2 — Decide (MANDATORY — no more control_ir ops)

After lint returns, your response MUST be a decide turn with zero `control_ir` ops. Do NOT write files. Do NOT lint again.

If lint returned errors (`passed: false`):
- Emit `control.type="rollback"` listing the lint issues verbatim as the reason
- The OS re-runs build_app with your feedback; build_app has the app_plan context to fix the files
- You MUST NOT write or delete files — you lack the app_plan context

If lint passed (`passed: true`), finish with an `app_builder_result` artifact:
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
