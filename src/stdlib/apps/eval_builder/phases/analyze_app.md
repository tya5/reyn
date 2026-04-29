---
type: phase
name: analyze_app
input: user_message
role: eval_designer
max_act_turns: 20
---

Read the target app's DSL files and design evaluation criteria for each phase.

## Step 0 — Note the running model

The ContextFrame `model` field contains the model class name (e.g. `standard`, `light`, `strong`)
or LiteLLM string currently running this phase.
Set `judge_model` in your output to that exact value — do NOT invent a model name.

## Step 1 — Extract app path from user_message

- `app_dsl_path`: the path to the target app's app.md (e.g. "reyn/project/writing_review_app/app.md")
- `dsl_root`: infer from the path (e.g. "reyn/" if path starts with "reyn/project/")
- If the path is missing, use ask_user to request it.

## Step 2 — Read DSL files

Use file read/glob ops to collect the app's full DSL:

1. Read `{app_dsl_path}` → get app name, entry phase, graph, final_output.
2. Glob `{app_dir}/**/*.md` and `{app_dir}/**/*.yaml` → list all phase and artifact files.
3. Read each phase `.md` and artifact `.yaml` (or `.md`) file.
4. For artifact types referenced by phases but not found locally, check `{dsl_root}shared/artifacts/{name}.yaml` or `{dsl_root}shared/artifacts/{name}.md`.

**CRITICAL**: You MUST read every artifact file (`.yaml` or `.md`) before designing criteria.
All field names in schema assertions MUST come from the `properties` keys you read — never invented.

## Step 3 — Design test cases

Design 1–2 realistic test cases:
- Case 1: a typical, well-formed input the app is designed to handle.
- Case 2 (if the app has review/revision loops): an input where the first draft is likely to be **rejected** — causing the review phase to rollback. Make the input deliberately ambiguous, underspecified, or contradictory so the reviewer is likely to reject it.

The goal is branch coverage: if the app has a rollback path, at least one test case should exercise it.

Each test case `input` must be a complete user_message string.

## Step 4 — Design evaluation criteria

### 4a. phase_eval_designs

One entry per phase in `phase_order`. Include ALL phases — even those with `can_finish: true`.

For each phase, split criteria into two kinds:

**schema** — a JSON Schema object (stored as a dict) that validates the artifact's `data` field.
- Use standard JSON Schema keywords: `type`, `required`, `properties`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, `enum`.
- The schema must be a valid JSON Schema object with at least `type: object` and `properties`.
- Cover: field existence (`required`), types (`type`), numeric ranges (`minimum`/`maximum`), non-empty arrays (`minItems`), exact expected values (`enum`), nested objects/arrays.
- Example:
  ```json
  {
    "type": "object",
    "required": ["score", "label"],
    "properties": {
      "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
      "label": {"type": "string", "enum": ["approved", "rejected"]},
      "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}
    }
  }
  ```

**quality** — LLM-judged content checks. Plain Japanese/English sentence.
- Use quality ONLY for checks that require reading and understanding content (e.g. "summary describes the app's purpose").
- Do NOT re-check what schema already covers (existence, type, range).
- 0–3 quality criteria per phase. Prefer 0–1 when schema covers the structure well.

### 4b. cross_phase_assertions

List equality checks between fields across phases. Format: `"phase_a.field == phase_b.field"`

Include when:
- An ID or filename produced in phase A must appear unchanged in phase B (e.g. `write_memo.filename == read_verify.filename`).
- A name decided in phase A must be reused in phase B (e.g. `plan_app.app_name == build_app.app_name`).

Leave empty if no such relationship exists.

### 4c. final_schema / final_quality

Same rules as 4a, applied to the app's declared `final_output` artifact.

## Criteria quality checklist (apply before finishing)

- [ ] Every field name in schema `properties` exists in the artifact `.yaml` I read — no invented fields.
- [ ] No quality criterion duplicates what the JSON Schema already covers (type, range, enum).
- [ ] Arrays that must be non-empty use `minItems: 1`.
- [ ] Boolean verdict fields with an expected value use `enum: [true]` or `enum: [false]`.
- [ ] Enum fields use `enum: [...]` with the correct allowed values.
- [ ] cross_phase_assertions covers all "should-be-same" relationships between phases.
- [ ] Quality criteria that are only evaluable when a specific runtime branch occurs (e.g. "if rollback is chosen...") are tagged `[aspirational]` — they cannot be reliably evaluated without guaranteeing that branch fires.
