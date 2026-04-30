---
type: phase
name: write_eval
input: skill_analysis
role: spec_writer
can_finish: true
allowed_ops: [file]
---

Generate the eval.md content, write it to the workspace, and report the result.

## Output path

Write to the skill's own directory: `{skill_dir}/eval.md`
Derive `skill_dir` from `skill_dsl_path` by removing the trailing `/skill.md`.
Example: if `skill_dsl_path` is `"reyn/local/article_generator/skill.md"`, write to `reyn/local/article_generator/eval.md`.

## eval.md format

Generate the file content exactly in this format:

```
---
type: eval
skill: {skill_dsl_path}
dsl_root: {dsl_root}
---

## case: {case.name}
input: "{case.input}"

### phase: {phase}
quality:
- {quality_criterion}
- [aspirational] {aspirational_criterion}

### phase: {next_phase}
quality:
- {quality_criterion}

## case: {case2.name}
input: "{case2.input}"
...
```

Rules:

- Each `## case:` block has exactly one `input:` line (a quoted string). Escape any internal `"` with `\"`.
- For each test case, write one `### phase: {phase_name}` section per entry in `phase_eval_designs` whose `quality` list is non-empty.
- Phase sections appear in `phase_order`.
- The `quality:` block is a YAML list — each item starts with `- `.
- A criterion that begins with `[aspirational]` keeps that prefix in the output (it controls scoring semantics).
- Repeat the full per-phase structure identically for each test case (same phases, same criteria text).
- Do NOT add `judge_model:`, `model:`, `schema:`, `### cross_phase`, or `### final` — they are not supported by the new eval skill.
- Do NOT add commentary or markdown outside the spec format.

## After writing the file

Compute the result fields directly from `skill_analysis`:

- `eval_md_path`: the workspace path you wrote.
- `case_count`: `len(test_cases)`.
- `criterion_count`: total quality criteria across all cases × phases (= `case_count × sum(len(d.quality) for d in phase_eval_designs if d.quality)`).
- `summary`: one sentence describing what was generated and how to run it. Include the `reyn eval <eval_md_path>` command.
