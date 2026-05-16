---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_improver]
---

# `skill_improver`

Iteratively improve an existing skill by running it under eval, planning DSL changes against the failing criteria, applying them, and re-evaluating until a score threshold is met or a stop condition fires.

## Entry

`prepare`

## Final output

`improvement_result` — score progression, files modified, and termination reason.

## When to use

- A skill's eval is below threshold and you want automated revision.
- You have a clear failure mode (specific criterion failing) and want targeted fixes.

## When NOT to use

- The skill doesn't have an eval spec yet — run [eval_builder](eval_builder.md) first.
- The change you want is structural (new phases, different graph) — `skill_builder` is more appropriate.

## Requirements

- Target skill must have an `eval.md` spec.

## Example

```bash
reyn run skill_improver "improve my_explainer"
```

## Source

[`src/reyn/stdlib/skills/skill_improver/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/skill_improver/skill.md)
