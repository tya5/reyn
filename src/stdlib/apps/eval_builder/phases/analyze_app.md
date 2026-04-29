---
type: phase
name: analyze_app
input: user_message
role: eval_designer
max_act_turns: 20
---

Read the target app's DSL files and design per-phase quality criteria.

## Step 1 — Extract app path from user_message

- `app_dsl_path`: the path to the target app's app.md (e.g. "reyn/project/writing_review_app/app.md")
- `dsl_root`: infer from the path (e.g. "reyn/" if path starts with "reyn/project/" or "reyn/local/")
- If the path is missing, use ask_user to request it.

## Step 2 — Read DSL files

Use file read/glob ops to collect the app's full DSL:

1. Read `{app_dsl_path}` → get app name, entry phase, graph, final_output.
2. Glob `{app_dir}/**/*.md` and `{app_dir}/**/*.yaml` → list all phase and artifact files.
3. Read each phase `.md` and artifact `.yaml` (or `.md`) file.
4. For artifact types referenced by phases but not found locally, check `{dsl_root}shared/artifacts/{name}.yaml` or `{dsl_root}shared/artifacts/{name}.md`.

**CRITICAL**: You MUST read every artifact file (`.yaml` or `.md`) before designing criteria.
Quality criteria reference artifact field semantics — they should match what the artifacts actually contain.

## Step 3 — Design test cases

Design 1–2 realistic test cases:
- Case 1: a typical, well-formed input the app is designed to handle.
- Case 2 (if the app has review/revision loops): an input where the first draft is likely to be **rejected** — causing the review phase to rollback. Make the input deliberately ambiguous, underspecified, or contradictory so the reviewer is likely to reject it.

The goal is branch coverage: if the app has a rollback path, at least one test case should exercise it.

Each test case `input` must be a complete user_message string.

## Step 4 — Design per-phase quality criteria

`phase_eval_designs` has one entry per phase in `phase_order`. Include ALL phases — even those with `can_finish: true`.

For each phase, write 1–4 `quality` criteria as plain sentences. Each criterion should:

- Describe a semantic property the phase's output artifact must satisfy that requires reading content (e.g. "summary describes the app's purpose", "review explicitly lists rejection conditions").
- Refer to fields that actually exist in the artifact (do not invent field names).
- Be evaluable by reading the artifact alone — if the criterion needs cross-phase context, omit it.

### `[aspirational]` tag

Prefix a criterion with `[aspirational]` when it represents a model capability ceiling rather than a fixable bug:

- Subjective judgments ("is specific", "is detailed") that consistently score below 1.0 even on correct output.
- "Gold standard" quality bars that go beyond the app's contract.
- Criteria that are only evaluable when a specific runtime branch fires (e.g. "if rollback is chosen ...") — these cannot be reliably tested without forcing that branch.

`[aspirational]` criteria are tracked but excluded from pass/fail.

## Criteria quality checklist (apply before finishing)

- [ ] Every artifact field referenced in a criterion exists in the artifact `.yaml` I read — no invented fields.
- [ ] Each criterion is evaluable by reading the phase's output artifact alone.
- [ ] Criteria that depend on a specific runtime branch firing are tagged `[aspirational]`.
- [ ] At most 4 criteria per phase; prefer fewer, sharper criteria over many vague ones.
