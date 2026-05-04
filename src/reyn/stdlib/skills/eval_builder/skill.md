---
type: skill
name: eval_builder
description: Build an eval spec (eval.md) — to run evaluations use the eval skill instead
entry: analyze_skill
final_output: eval_spec_result
final_output_description: |
  Path to the generated eval.md plus case/criterion counts and a brief summary.
  The user runs the spec separately with `reyn eval <eval_md_path>`.
finish_criteria:
  - eval.md has been written at the eval_output_path resolved by the OS
  - eval_spec_result captures the path, case count, and criterion count
graph:
  analyze_skill: [write_eval]
routing:
  intents: [task]
  when_to_use:
    - User wants to *create* / *build* / *generate* an eval spec (eval.md) for a skill
    - User asks to scaffold or write eval criteria for a skill
    - Typical input form is "SKILL_NAME の eval を作って" or "eval.md を生成して"
  when_not_to_use:
    - User wants to *run* or *execute* an evaluation against existing criteria — use eval skill
    - Intent is "eval を実行する" or "SKILL_NAME を eval して" — use eval skill, not eval_builder
    - eval_builder creates the spec; eval runs it — for "eval して" choose eval, not eval_builder
    - Conceptual questions about evaluation (stable_knowledge)
  examples:
    positive:
      - "direct_llm の eval を作って"
      - "X skill 用の eval を作って"
      - "eval.md を生成して"
      - "このスキルのテスト基準を書いて"
    negative:
      - "direct_llm を eval して"
      - "skill X を eval して"
      - "eval ってなに？"
permissions:
  # analyze_skill reads target skill DSL files which may live under
  # src/reyn/stdlib/skills/ — outside the project root when running from a
  # worktree. Declare recursive read access for all three skill search paths
  # (B8-NEW-1 pattern, same as skill_improver).
  file.read:
    - path: src/reyn/stdlib/skills
      scope: recursive
    - path: reyn/local
      scope: recursive
    - path: reyn/project
      scope: recursive
  python:
    - module: ./analyze_skill_resolver.py
      function: compute_paths
      mode: trusted
      timeout: 5
    - module: ./analyze_skill.py
      function: inject_resolved_paths
      mode: pure
      timeout: 5
---

## Overview

Reads a target skill's DSL files and generates a per-phase, LLM-judged quality-criteria
`eval.md` spec. Does not run the spec — invoke `reyn eval` separately.

`analyze_skill` reads every phase and artifact file in the target skill, designs 2–3
representative test cases (typical input, plus a rollback case if the skill has a
review loop), and writes 1–4 quality criteria per phase. `write_eval` formats the
result into `eval.md` and writes it to the OS-resolved output path.

## Phase flow

```
analyze_skill  →  write_eval
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `analyze_skill` | eval_designer | Reads the skill's DSL files; designs test cases and per-phase quality criteria |
| `write_eval`  | spec_writer   | Formats the criteria into `eval.md` and writes it to the resolved output path |

## Input

Pass the **skill name** (not a path). The OS resolves the path via `resolve_skill_path`.

**Natural-language form** (preferred for CLI):
```
reyn run eval_builder "Generate spec for skill named my_skill"
```

**Structured form** (used by callers such as skill_improver):
```
reyn run eval_builder '{"type":"eval_builder_request","data":{"target_skill":"my_skill"}}'
```

Both forms work end-to-end. The `analyze_skill` preprocessor accepts either artifact
type and extracts the skill name without requiring the LLM to construct paths.

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

Each criterion is an LLM-judged sentence describing a semantic property of the phase's
output artifact. Required (default) criteria count against the score; `[aspirational]`
criteria are tracked but excluded from pass/fail. The judge model is set in `judge_phase`
(the `model_class` of its `judge` phase) — not configurable per-spec.

## Output

`eval_spec_result` reports the path of the generated `eval.md`, the case count, the
total criterion count, and a brief summary including the next-step command:

```
reyn eval reyn/local/my_skill/eval.md --model standard
```

Combine with `skill_improver` for a tight quality-improvement cycle: build a spec with
`eval_builder` → improve with `skill_improver` → measure with `reyn eval`.
`skill_improver` will auto-invoke `eval_builder` if the spec is missing.
