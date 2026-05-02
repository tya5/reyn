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

Derive `skill_dir` from `skill_dsl_path` by removing the trailing `/skill.md`.

**If `skill_dir` starts with `src/` or any path outside `reyn/` and `.reyn/`** (e.g. stdlib skills), you cannot write there — the runtime denies it. Instead write to `reyn/local/<skill_name>/eval.md` where `skill_name` is the last path component of `skill_dir`.

**Otherwise** write to `{skill_dir}/eval.md` directly.

Examples:
- `skill_dsl_path` = `"reyn/local/article_generator/skill.md"` → write to `reyn/local/article_generator/eval.md`
- `skill_dsl_path` = `"src/stdlib/skills/word_stats_demo/skill.md"` → write to `reyn/local/word_stats_demo/eval.md`

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
- For each test case, write one `### phase: {phase_name}` section per entry in that case's `phase_criteria` array whose `quality` list is non-empty.
- Phase sections appear in `phase_order`.
- The `quality:` block is a YAML list — each item starts with `- `.
- A criterion that begins with `[aspirational]` keeps that prefix in the output (it controls scoring semantics).
- Use each case's own `phase_criteria` — do NOT copy criteria from one case to another. Each case was designed with different criteria that probe different behavior.
- Do NOT add `judge_model:`, `model:`, `schema:`, `### cross_phase`, or `### final` — they are not supported by the new eval skill.
- Do NOT add commentary or markdown outside the spec format.

If you receive `[denied]` on a write op, do NOT retry the same path. Abort immediately with a clear explanation.

## After writing the file

Compute the result fields directly from `skill_analysis`:

- `eval_md_path`: the workspace path you wrote.
- `case_count`: `len(test_cases)`.
- `criterion_count`: total quality criteria across all cases × phases (= `case_count × sum(len(d.quality) for d in phase_eval_designs if d.quality)`).
- `summary`: one sentence describing what was generated and how to run it. Include the `reyn eval <eval_md_path>` command.
