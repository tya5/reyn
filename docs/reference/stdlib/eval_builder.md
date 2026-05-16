---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [eval_builder]
---

# `eval_builder`

Auto-generate an eval spec (`eval.md`) for a skill.

## Entry

`analyze_skill`

## Final output

`eval_spec_result` — path to the generated `eval.md`, case count, criterion count, and a summary.

## How it works

Reads the target skill's `skill.md` and phase files, infers test cases that exercise the graph, and proposes per-phase quality criteria. The user runs the spec separately with `reyn eval <eval_md_path>`.

## When phases use Python preprocessors

`eval_builder` writes DO/DON'T templates for criteria when a phase has a Python step — this avoids "vacuously true" criteria like "char_count is correct" that the LLM judge can't actually verify.

## Example

```bash
reyn run eval_builder "build an eval for my_explainer"
```

## Source

[`src/reyn/stdlib/skills/eval_builder/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/eval_builder/skill.md)
