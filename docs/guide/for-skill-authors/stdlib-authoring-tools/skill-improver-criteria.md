---
type: agent
topic: stdlib
audience: [agent]
applies_to: [skill_improver]
---

# `skill_improver` — review criteria

Use this list when you (the `skill_improver` skill) audit an existing skill. Each criterion maps to one or more of the [P1–P8 principles](../../../concepts/principles.md). Findings should be specific, file-anchored, and actionable.

## Phase markdown

For every `phases/<name>.md`:

- [ ] Frontmatter declares **only** `input` (and optionally `preprocessor`, `role`, `can_finish`). No output schema. [P1]
- [ ] No enumeration of next-phase artifact fields in the body. [P1, P8]
- [ ] No description of Control IR format (e.g. "emit `{kind: file, op: write, ...}`"). [P8]
- [ ] No reference to a sibling phase's name. Transitions live in the skill graph. [P1]
- [ ] Body covers WHAT to do, WHEN to pick which candidate, and domain rules — nothing else.
- [ ] If the phase has `can_finish: true`, instructions clearly state when to finish vs. continue.

## Skill markdown

For `skill.md`:

- [ ] `entry`, `graph`, `final_output` all present. [P2]
- [ ] `final_output` is a single artifact type.
- [ ] Every phase referenced in `graph` has a corresponding `phases/<name>.md`.
- [ ] Every phase whose `can_finish: true` has a path to `end` in the graph.
- [ ] No unreachable phases.
- [ ] No self-loops (graph entries listing themselves).

## Artifact schemas

For each `artifacts/<name>.yaml`:

- [ ] One artifact = one purpose. No kitchen-sink shapes.
- [ ] `required` is minimal; optional fields are marked optional.
- [ ] Field names are lowercase_snake_case.
- [ ] No "quality_notes" / "revision_reason" / other meta-feedback fields baked in. [P7]
- [ ] No decision fields with skill-specific values (`revise`, `redo`). [P7]

## Preprocessor

For each preprocessor step:

- [ ] The step kind is one of `run_skill`, `iterate`, `validate`, `lint_plan`, `python`.
- [ ] `into` doesn't collide with an existing input artifact key.
- [ ] If `python`: matching `permissions.python` entry, `.py` file exists, function defined, `output_schema` declared.
- [ ] If `python` `mode: safe`: no banned constructs in the AST (`open`, `eval`, `subprocess`, etc.).

## Eval-friendliness

- [ ] At least one path produces an output that can be judged against rubric criteria.
- [ ] Phase boundaries align with what a judge would assess (one phase = one judgeable output).
- [ ] No criteria that would be vacuously satisfied by any reasonable LLM output.

## Output of a review

For each finding:

```
[severity]  <file>:<line or section>
  Issue:    <what's wrong>
  Why:      <which principle / pitfall this hits>
  Fix:      <concrete change>
```

Severity:

- `error` — violates P1–P8 or breaks linting; must fix.
- `warning` — likely cause of bugs but not a hard violation.
- `info` — stylistic or polish-level.

## After auditing

- [ ] Run `reyn lint <skill>` and include any new findings in the report.
- [ ] If proposing changes, write them as diffs the user can review (don't silently rewrite).
- [ ] Don't claim "ready" until errors are resolved or explicitly waived.
