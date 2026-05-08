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

Use `skill_analysis.eval_output_path` directly — the OS preprocessor has already
resolved the correct write destination (including the stdlib redirect to
`reyn/local/<name>/eval.md`). Do NOT re-derive the path from `skill_dsl_path`.

Do NOT apply any path-derivation logic here. The preprocessor in `analyze_skill`
called `resolve_skill_path` and stored the result; this phase is a pure formatter.

## eval.md format

Generate the file content exactly in this format:

```
---
type: eval
skill: {skill_analysis.skill_dsl_path}
skill_root: {skill_analysis.skill_root}
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

## Evidence-bound audit (apply while writing each criterion)

As you write each criterion, audit it: could a structurally-correct but content-empty output satisfy it? If yes, add an evidence clause — require the output to contain something specific derived from the input. See [`eval-builder-rubric.md`](../../../../docs/guide/for-skill-authors/eval-builder-rubric.md) Principle 5 for examples.

If a criterion is genuinely shape-only (e.g. "the output contains at least one bullet") and no evidence clause is possible, include it but note it in `summary` so the human reviewer can inspect it. Shape-only criteria are not forbidden — but they should be conscious choices, not defaults.

If you receive `[denied]` on a write op, do NOT retry the same path. Abort immediately with a clear explanation.

## After writing the file

Compute the result fields directly from `skill_analysis`:

- `eval_md_path`: `skill_analysis.eval_output_path` (the path you just wrote).
- `case_count`: `len(test_cases)`.
- `criterion_count`: total quality criteria across all cases × phases (= `case_count × sum(len(d.quality) for d in phase_eval_designs if d.quality)`).
- `summary`: one sentence describing what was generated and how to run it. Include the `reyn eval <eval_md_path>` command.
