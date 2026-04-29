---
type: skill
name: eval_builder
description: Auto-generate an eval spec (eval.md) for a skill
entry: analyze_app
final_output: eval_spec_result
final_output_description: |
  Path to the generated eval.md plus case/criterion counts and a brief summary.
  The user runs the spec separately with `reyn eval <eval_md_path>`.
finish_criteria:
  - eval.md has been written next to the target skill's skill.md
  - eval_spec_result captures the path, case count, and criterion count
graph:
  analyze_app: [write_eval]
---

## Overview

Reads a target skill's DSL files and generates a per-phase, LLM-judged quality-criteria `eval.md` spec. Does not run the spec — invoke `reyn eval` separately.

`analyze_app` reads every phase and artifact file in the target skill, designs 1–2 representative test cases (typical input, plus a rollback case if the skill has a review loop), and writes 1–4 quality criteria per phase. `write_eval` formats the result into `eval.md` and writes it alongside the target's `skill.md`.

## Phase flow

```
analyze_app  →  write_eval
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `analyze_app` | eval_designer | Reads the skill's DSL files; designs test cases and per-phase quality criteria |
| `write_eval`  | spec_writer   | Formats the criteria into `eval.md` and writes it next to the target skill |

## Input

A natural-language sentence including the target skill's `skill.md` path. If the path is unclear, `ask_user` will request it.

```
reyn run eval_builder "Generate an eval.md for reyn/local/my_skill/skill.md"
```

## Generated eval.md structure

```markdown
---
type: eval
skill: reyn/local/my_skill/skill.md
dsl_root: reyn/local/
---

## case: typical_input
input: "A realistic user message for this skill"

### phase: analyze
quality:
- Each issue contains a concrete improvement suggestion
- [aspirational] Suggestions reference specific lines or code regions

### phase: review
quality:
- Verdict matches the issues listed in the analysis
```

Each criterion is an LLM-judged sentence describing a semantic property of the phase's output artifact. Required (default) criteria count against the score; `[aspirational]` criteria are tracked but excluded from pass/fail. The judge model is set in `judge_phase` (the `model_class` of its `judge` phase) — not configurable per-spec.

## Output

`eval_spec_result` reports the path of the generated `eval.md`, the case count, the total criterion count, and a brief summary including the next-step command:

```
reyn eval reyn/local/my_skill/eval.md --model standard
```

Combine with `skill_improver` for a tight quality-improvement cycle: build a spec with `eval_builder` → improve with `skill_improver` → measure with `reyn eval`. `skill_improver` will auto-invoke `eval_builder` if the spec is missing.
