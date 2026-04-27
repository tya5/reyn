---
type: phase
name: write_eval
input: app_analysis
input_description: |
  Structured analysis of the target app: app_dsl_path, dsl_root, app_name,
  judge_model, phase_order, test_cases, phase_eval_designs (each with schema[] and quality[]),
  cross_phase_assertions, final_schema, final_quality.
role: spec_writer
---

Generate the eval.md content and write it to the workspace.

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
- Subjective judgements ("具体的である", "詳細である") that consistently score below 1.0 even on correct output
- Comparative checks ("revision is better than draft") that require cross-artifact reasoning
- "Gold standard" quality bars that go beyond what the app is required to produce

Example:
```
quality:
- issues の各項目が存在する                          ← required (default, no tag)
- [aspirational] issues の各項目が非常に具体的な改善案を含む
```

## After writing

Set in the output artifact:
- `eval_md_path`: the workspace-relative path where you wrote the file.
- `app_dsl_path`: the target app DSL path from app_analysis.
- `model`: the judge_model from app_analysis (this is passed to eval_runner to run the target app).
- `case_count`: number of test cases.
- `total_criteria`: total lines (schema assertions + quality criteria) across all cases and phases.
- `next_steps`: tell the user:
  1. The file was written to `workspace/{eval_md_path}`.
  2. The eval will now run automatically via eval_runner.
  3. To run it again manually: `agent-os eval --spec {resolved_cwd_relative_path} --model <model>`.
  4. If the target app is in the project `dsl/` tree (not in workspace), also copy the file:
     `cp workspace/{eval_md_path} {app_dir}/eval.md`
