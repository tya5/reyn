---
type: phase
name: design_artifacts
input: skill_structure
role: schema_designer
model_class: standard
preprocessor:
  - type: lint_plan
    over: data
    into: data.lint_issues
---

Design JSON Schema definitions for every artifact in the skill plan.

## Step 0 — Check structural lint findings

`data.lint_issues` is populated by the OS preprocessor with deterministic structural checks on the upstream `skill_structure` (graph cycles, transition targets, artifact coverage, entry phase validity).

If `data.lint_issues` is non-empty, the plan itself is structurally broken. You CANNOT fix these issues by adding schemas — they originate from `plan_skill`. Emit `control.type="rollback"` immediately, with `control.reason.summary` quoting every entry from `lint_issues` so plan_skill can correct them. Do NOT design schemas in this case.

If `data.lint_issues` is empty, proceed to Step 1.

## Step 1 — Design schemas

If rollback context is present in the conversation history, read the rejection feedback carefully and address every issue before redesigning schemas.

For each artifact in `artifacts` and for `final_output`, produce a JSON Schema object:
- Always use `type: object` at the top level with `properties` and `required`
- Add a `description` to every field — this is how the LLM understands what to populate
- Use `"enum": [...]` when a field has a fixed set of valid values
- Use `"minimum"`/`"maximum"` for numeric ranges
- For arrays of strings: `{"type": "array", "items": {"type": "string"}}`
- For arrays of objects: `{"type": "array", "items": {"type": "object", "properties": {...}, "required": [...]}}`

Schema design principles:
- **Intermediate artifacts** (passed into a review phase): must include all context the reviewer needs to make an informed judgment. Do NOT rely on the reviewer inferring context from prior phases — if information is needed, it must be in the artifact.
- **Review verdict artifacts** (output of a review phase): contain only verdict fields (e.g. `status`, `score`, `feedback`, `rejection_reason`) — NOT a copy of the content being reviewed.
- **Final output artifacts**: contain the deliverable the user receives. If the workflow approves content, include the skillroved content itself plus any required verdict fields (e.g. `status: "approved"`).
- Keep schemas focused: prefer fewer, well-named fields over many redundant ones.

Output a complete `skill_plan` carrying over all fields from the input, with `schema` added to each artifact and to `final_output`. Set `review_notes` to empty string.
