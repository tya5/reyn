---
type: phase
name: review_plan
input: skill_plan
role: plan_reviewer
model_class: strong
---

Review the skill plan for schema quality before DSL files are written.
Structural checks (DAG, reachability, artifact coverage) are handled deterministically by the linter after build — focus only on what requires judgement.

## Schema checks

**Top-level type**: every artifact schema must have `type: object`.

**Fields have descriptions**: every property in every schema must have a `description` field.

**Required fields declared**: every schema must have a non-empty `required` array.

**Review artifacts are focused**: schemas for review phases should contain only verdict fields — not content duplicated from the artifact being reviewed.

## Output

- If issues found: emit `control.type="rollback"` with `control.reason.summary` listing all problems as a numbered list. artifact may be empty.
- If all clear: transition to `build_skill` with `control.type="transition"` and the `skill_plan` artifact unchanged.
