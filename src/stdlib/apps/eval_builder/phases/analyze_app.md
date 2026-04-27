---
type: phase
name: analyze_app
input: user_message
input_description: |
  Natural language request specifying the target app to build an eval spec for.
  Must include the app DSL path. Example:
  "dsl/apps/writing_review_app/app.md の eval.md を作って"
  "Create an eval spec for dsl/apps/architecture_analyzer/app.md — focus on article quality"
role: eval_designer
max_act_turns: 20
---

Read the target app's DSL files and design evaluation criteria for each phase.

## Step 0 — Note the running model

The ContextFrame `model` field contains the model class name (e.g. `standard`, `light`, `strong`)
or LiteLLM string currently running this phase.
Set `judge_model` in your output to that exact value — do NOT invent a model name.

## Step 1 — Extract app path from user_message

- `app_dsl_path`: the path to the target app's app.md (e.g. "dsl/apps/writing_review_app/app.md")
- `dsl_root`: infer from the path (e.g. "dsl/" if path starts with "dsl/apps/")
- If the path is missing, use ask_user to request it.

## Step 2 — Read DSL files

Use file read/glob ops to collect the app's full DSL:

1. Read `{app_dsl_path}` → get app name, entry phase, graph, final_output.
2. Glob `{app_dir}/**/*.md` (where app_dir is the directory containing app.md) → list all phase and artifact files.
3. Read each phase .md and artifact .md file.
4. For artifact types referenced by phases but not found locally, check `{dsl_root}shared/artifacts/{name}.md`.

**CRITICAL**: You MUST read every artifact .md file before designing criteria.
All field names in schema assertions MUST come from what you read — never invented.

## Step 3 — Design test cases

Design 1–2 realistic test cases:
- Case 1: a typical, well-formed input the app is designed to handle.
- Case 2 (if the app has review/revision loops): an input where the first draft is likely to need revision.

Each test case `input` must be a complete user_message string.

## Step 4 — Design evaluation criteria

### 4a. phase_eval_designs

One entry per phase in `phase_order`. Include ALL phases — even those with `can_finish: true`.

For each phase, split criteria into two kinds:

**schema** — deterministic assertions about field structure. Format: `"field_path: type[, constraint]"`
- `field_path`: dot-notation path into artifact data (e.g. `review_result.score`)
- `type`: one of `string`, `number`, `integer`, `boolean`, `array`, `object`
- constraints (optional, comma-separated after type):
  - `range 0.0-1.0` — numeric range (inclusive)
  - `min N` — numeric min value, or minimum array length
  - `max N` — numeric max value, or maximum array length
  - `min_length N` — minimum string/array length
  - `equals "value"` — exact string match
  - `equals true` / `equals false` — exact boolean match
  - `contains "text"` — substring in string, or any-element-contains in array
- Use schema for: field existence, type correctness, numeric ranges, non-empty arrays, exact expected values, required file names in lists.
- 2–5 schema assertions per phase.

**quality** — LLM-judged content checks. Plain Japanese/English sentence.
- Use quality ONLY for checks that require reading and understanding content (e.g. "summary がアプリの目的を説明している").
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

- [ ] Every field name in schema assertions exists in the artifact .md I read.
- [ ] No quality criterion duplicates what a schema assertion already checks.
- [ ] `issues` / `problems` / `errors` arrays have `min 1` if the app should always produce at least one.
- [ ] Boolean verdict fields have `equals true` or `equals false` when a specific value is expected.
- [ ] cross_phase_assertions covers all "should-be-same" relationships between phases.
