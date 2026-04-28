---
type: phase
name: review_plan
input: app_plan
role: plan_reviewer
model_class: strong
---

Review the app plan for structural consistency and schema quality before DSL files are written.

Check each of the following. If ANY issue is found, transition to `design_artifacts` with `review_notes` listing all problems. If all checks pass, transition to `build_app` with `review_notes` set to empty string.

## Structural checks

**Graph reachability**: every phase except `entry_phase` must appear as a destination in at least one transition edge. A phase with no incoming edge is unreachable.

**Artifact coverage**: every phase's `input_artifact` must appear in `artifacts[]` by name, OR be `user_message` (stdlib). No phase may reference an undefined artifact.

**No orphaned artifacts**: every artifact in `artifacts[]` must be referenced as `input_artifact` by at least one phase. Unused artifacts indicate a planning error.

**Transition targets are phases**: every name in any `to` list must be a phase name from `phases[]`. Artifact names and final_output names are not valid transition targets.

## Schema checks

**Top-level type**: every artifact schema must have `type: object`.

**Fields have descriptions**: every property in every schema must have a `description` field.

**Required fields declared**: every schema must have a non-empty `required` array.

**Review artifacts are focused**: schemas for review phases should contain only verdict fields — not content duplicated from the artifact being reviewed.

## Output

Output the `app_plan` unchanged except for setting `review_notes`:
- If issues found: set `review_notes` to a numbered list of all problems found, then transition to `design_artifacts`
- If all clear: set `review_notes` to `""`, then transition to `build_app`
