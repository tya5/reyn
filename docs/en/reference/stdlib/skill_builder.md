---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_builder]
---

# `skill_builder`

Generate a new skill from a natural-language description.

## Purpose

Plan, design artifacts, write DSL files, and lint the result. Optionally revises if linting fails.

## Entry

`plan_skill`

## Final output

`skill_builder_result` — name, path, files written, and lint outcome.

## When to use

- You have a clear idea for a skill but don't want to hand-write the DSL.
- You want a 2–5 phase skill with simple linear or branching flow.

## When NOT to use

- You already have a skill that's close — use [skill_improver](skill_improver.md) instead.
- You're importing from another framework — use [skill_importer](skill_importer.md).

## Example

```bash
reyn run skill_builder "A skill that takes a topic and returns a one-paragraph explainer. Two phases: outline (3 bullets) then expand (paragraph)."
```

## Source

[`src/stdlib/skills/skill_builder/skill.md`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/skill_builder/skill.md)
