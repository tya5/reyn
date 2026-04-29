---
type: phase
name: write_eval
input: app_analysis
role: spec_writer
can_finish: true
---

Generate the eval.md content and write it to the workspace, then run it.

## Output path

Write to the app's own directory: `{app_dir}/eval.md`
Derive `app_dir` from `app_dsl_path` by removing the trailing `/app.md`.
Example: if `app_dsl_path` is `"reyn/local/article_generator/app.md"`, write to `reyn/local/article_generator/eval.md`.

## eval.md format

Generate the file content exactly in this format:

```
---
type: eval
app: {app_dsl_path}
dsl_root: {dsl_root}
judge_model: {judge_model}
---

## case: {case.name}
input: "{case.input}"

### phase: {phase}
schema:
  type: object
  required: [field1, field2]
  properties:
    field1:
      type: string
    field2:
      type: array
      minItems: 1
      items:
        type: string

quality:
- {quality_criterion}

### cross_phase
- {phase_a.field == phase_b.field}

### final
schema:
  type: object
  required: [field1]
  properties:
    field1:
      type: string

quality:
- {final_quality_criterion}

## case: {case2.name}
input: "{case2.input}"
...
```

Rules:
- Each `## case:` block has exactly one `input:` line (quoted string).
- Phase sections use `### phase: {phase_name}` — actual phase name from phase_order.
- Only include phases that have entries in phase_eval_designs.
- Under each `### phase:` write a `schema:` block: serialize `phase_eval_designs[phase].schema` (a JSON Schema object) as YAML, indented under `schema:`. Omit the block if schema is null or empty.
- The `schema:` block must be a valid JSON Schema object in YAML. Do NOT write `- field: type` bullet lines — use nested YAML key-value pairs.
- Write a `quality:` block for quality criteria (omit if quality list is empty). Quality lines start with `- `.
- Write `### cross_phase` only if cross_phase_assertions is non-empty. Place it after all `### phase:` sections and before `### final`.
- Write `### final` using final_schema (serialized as YAML under `schema:`) and final_quality. Omit `schema:` or `quality:` sub-block if the respective value is empty.
- Repeat the full phase+cross_phase+final structure identically for each test case (same structure, same criteria text).
- Do NOT add extra commentary or markdown outside the spec format.

## Quality criterion tags

Each quality criterion line may carry an optional tag prefix:

- `[required]` — must pass; failure counts against the score. This is the default (no prefix = required).
- `[aspirational]` — tracked but excluded from score; failure is informational only.

Use `[aspirational]` for criteria that represent a model capability ceiling rather than a fixable bug:
- Subjective judgements ("is specific", "is detailed") that consistently score below 1.0 even on correct output
- Comparative checks ("revision is better than draft") that require cross-artifact reasoning
- "Gold standard" quality bars that go beyond what the app is required to produce

## After writing the file

Run the eval op against the written spec using the model from app_analysis:

```json
{"kind": "eval", "spec_path": "<eval_md_path>", "model": "<app_analysis.model>"}
```

The eval op result appears in `control_ir_results`. Read these fields directly:
- `overall_score` → eval_result.overall_score
- `passed` → eval_result.passed
- `passed_criteria` → eval_result.passed_criteria
- `total_criteria` → eval_result.total_criteria
- `weakest_phase` → eval_result.weakest_phase
- `spec_path` → eval_result.spec_path

Whether eval passes or fails, always finish with a populated `eval_result` artifact — never use null for any required field.

summary should describe results: e.g. "All 12 criteria passed (score 1.00)." or "6/12 criteria passed (score 0.50) — weakest: analyze."

To re-run manually:
```
reyn eval {app_dir}/eval.md --model <model>
```
