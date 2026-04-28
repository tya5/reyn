---
type: phase
name: write_eval
input: app_analysis
role: spec_writer
can_finish: true
---

Generate the eval.md content and write it to the workspace, then run it.

## Output path

Write to: `eval_specs/{app_name}/eval.md`
(workspace-relative; e.g. `eval_specs/writing_review_app/eval.md`)

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
- {schema_assertion}
- {schema_assertion}

quality:
- {quality_criterion}

### cross_phase
- {phase_a.field == phase_b.field}

### final
schema:
- {final_schema_assertion}

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
- Under each `### phase:` write a `schema:` block (even if empty — omit the block only if schema list is truly empty) followed by a `quality:` block (omit if quality list is empty).
- Write `### cross_phase` only if cross_phase_assertions is non-empty. Place it after all `### phase:` sections and before `### final`.
- Write `### final` using final_schema and final_quality. Omit `schema:` or `quality:` sub-block if the respective list is empty.
- Criteria and schema assertion lines start with `- ` (hyphen space).
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

If eval passes, finish with an `eval_result` artifact populated from the eval op result.
If eval fails (passed: false), still finish — report the scores and weakest_phase so the user knows what needs improvement.

summary should describe results: e.g. "All 12 criteria passed (score 1.00)." or "6/12 criteria passed (score 0.50) — weakest: analyze."
