---
type: skill
name: skill_builder
description: Generate a new skill from a natural-language description
entry: plan_skill
final_output: skill_builder_result
final_output_description: |
  Build result for the generated skill: name, path, files written, and lint outcome.
finish_criteria:
  - All DSL files for the skill have been written to the workspace
  - The generated DSL has been linted with no errors
graph:
  plan_skill: [design_artifacts]
  design_artifacts: [review_plan]
  review_plan: [build_skill]
  build_skill: [verify_skill]
routing:
  intents: [task]
  when_to_use:
    - User describes a new skill they want to build / generate / create
    - Input is a natural-language description of desired skill behavior
  when_not_to_use:
    - User wants to modify an existing skill (use skill_improver)
    - User wants to import an existing skill (use skill_importer)
    - Conceptual questions about how skill DSL works (stable_knowledge)
  examples:
    positive:
      - "ブログ記事を書く skill を作って"
      - "X するスキルを生成して"
      - "新しい skill を作りたい: 〜"
    negative:
      - "skill って何？"
      - "DSL の書き方を教えて"
      - "既存のスキルを改善して"   # this is skill_improver, not skill_builder
---

## Overview

Describe a skill's purpose in plain language and `skill_builder` generates a full phase / artifact / graph design, writes the DSL files, and verifies the output with the linter. Generated files are immediately runnable with `reyn run`.

## Input

A natural-language description of the skill you want to build. Minimum information: what the skill does and what its input/output look like.

```
reyn run skill_builder "Build a skill that writes an article and reviews it before delivery"
```

Alternatively, pass a structured `skill_request` artifact with `skill_name`, `description`, and `goal`.

If you are unsure about the skill name, write "suggest some name options" and the builder proposes candidates. Anything else missing is requested via `ask_user`.

## Phase flow

```
plan_skill  →  design_artifacts  →  review_plan  →  build_skill  →  verify_skill
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `plan_skill` | architect | Designs phases, transitions, and overall structure |
| `design_artifacts` | schema_designer | Adds JSON Schemas to each artifact and the final output |
| `review_plan` | reviewer | Sanity-checks the plan before any files are written |
| `build_skill` | dsl_writer | Writes `skill.md`, `phases/*.md`, and `artifacts/*.yaml` |
| `verify_skill` | verifier | Runs the linter; rolls back to `build_skill` if errors are found |

The OS preprocessor injects deterministic structural lint hints into `design_artifacts` (graph cycles, transition targets, artifact coverage, entry-phase validity) so issues surface before file generation.

`plan_skill` selects one of three patterns based on the input:

| Pattern | Structure | Best for |
|---------|-----------|----------|
| A: Review loop | generate → review → deliver | Content generation requiring subjective judgment |
| B: Research first | research → generate → review → deliver | When information gathering must precede generation |
| C: Simple linear | process → deliver | Deterministic transformations and classification |

## Output

`skill_builder_result` reports the generated skill's name, path, the files written, and lint outcome. Files land under `reyn/local/{skill_name}/`:

```
reyn/local/{skill_name}/
  skill.md                 ← Skill definition (entry, graph, final_output)
  phases/{phase_name}.md   ← Per-phase definitions
  artifacts/{name}.yaml    ← Per-artifact JSON Schemas
```

When `lint_passed: true`, the skill is ready to run:

```
reyn run <skill_name> "<your input>"
```

`verify_skill` rolls back on lint errors, so a successful result means the DSL passed lint. If the quality of a generated skill is low, pass it to `skill_improver` to auto-improve phase instructions and artifact schemas against an eval.
