---
type: phase
name: verify_skill
input: build_result
role: dsl_verifier
can_finish: false
max_act_turns: 1
allowed_ops: [read_file, write_file, edit_file, delete_file, glob_files, grep_files, lint]
---

## Step 1 — Run lint (your ONLY act turn)

Issue exactly one lint op:

```
{"kind": "lint", "skill_path": "<data.skill_path>"}
```

## Step 2 — Decide (MANDATORY — no more control_ir ops)

After lint returns, your response MUST be a decide turn with zero `control_ir` ops. Do NOT write files. Do NOT lint again.

If lint returned errors (`passed: false`):
- Emit `control.type="rollback"` listing the lint issues verbatim as the reason
- The OS re-runs build_skill with your feedback; build_skill has the skill_plan context to fix the files
- You MUST NOT write or delete files — you lack the skill_plan context

If lint passed (`passed: true`), transition to `iterate_with_evals` with a `skill_builder_result` artifact:
- `skill_name`: from data.skill_name
- `skill_path`: from data.skill_path
- `files_written`: from data.files_written
- `file_count`: from data.file_count
- `lint_passed`: true
- `lint_issues`: []
- `summary`: one sentence describing what the skill does for its users
- `max_iterations`: from data.max_iterations (= copy through; 0 if absent)
- `score_threshold`: from data.score_threshold (= copy through; 0.85 if absent)
- (do NOT populate `improvement_summary` — the next phase owns it)

summary MUST describe what the skill does for its users — not what you (the builder) did.
Good: "A skill that lets users submit documents for reviewer approval or rejection with reasons."
Bad: "Generated DSL files for the review skill and saved them to the workspace."

The terminal phase is now `iterate_with_evals`. When `max_iterations` is 0
(= the default), that phase short-circuits and finishes with the artifact you
produced here unchanged. When `max_iterations` is > 0, it chains into
`skill_improver` for the eval-driven loop.
