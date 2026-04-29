---
type: skill
name: skill_builder
description: Generate a new skill from a natural-language description
entry: plan_app
final_output: skill_builder_result
final_output_description: |
  Build result for the generated skill: name, path, files written, and lint outcome.
finish_criteria:
  - All DSL files for the skill have been written to the workspace
  - The generated DSL has been linted with no errors
graph:
  plan_app: [design_artifacts]
  design_artifacts: [review_plan]
  review_plan: [build_app]
  build_app: [verify_app]
---

## Overview

Describe a skill's purpose in plain language and `skill_builder` generates a full phase / artifact / graph design, writes the DSL files, and verifies the output with the linter. Generated files are immediately runnable with `reyn run`.

## Input

A natural-language description of the skill you want to build. Minimum information: what the skill does and what its input/output look like.

```
reyn run skill_builder "Build a skill that writes an article and reviews it before delivery"
```

Alternatively, pass a structured `app_request` artifact with `app_name`, `description`, and `goal`.

If you are unsure about the skill name, write "suggest some name options" and the builder proposes candidates. Anything else missing is requested via `ask_user`.

## Phase flow

```
plan_app  →  design_artifacts  →  review_plan  →  build_app  →  verify_app
```

| Phase | Role | Responsibility |
|-------|------|----------------|
| `plan_app` | architect | Designs phases, transitions, and overall structure |
| `design_artifacts` | schema_designer | Adds JSON Schemas to each artifact and the final output |
| `review_plan` | reviewer | Sanity-checks the plan before any files are written |
| `build_app` | dsl_writer | Writes `skill.md`, `phases/*.md`, and `artifacts/*.yaml` |
| `verify_app` | verifier | Runs the linter; rolls back to `build_app` if errors are found |

The OS preprocessor injects deterministic structural lint hints into `design_artifacts` (graph cycles, transition targets, artifact coverage, entry-phase validity) so issues surface before file generation.

`plan_app` selects one of three patterns based on the input:

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

`verify_app` rolls back on lint errors, so a successful result means the DSL passed lint. If the quality of a generated skill is low, pass it to `skill_improver` to auto-improve phase instructions and artifact schemas against an eval.
