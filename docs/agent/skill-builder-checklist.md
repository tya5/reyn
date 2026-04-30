---
type: agent
topic: dsl
audience: [agent]
applies_to: [skill_builder]
---

# `skill_builder` — design checklist

Use this checklist whenever you (the `skill_builder` skill) plan or write a new skill. Every item maps to one or more of the [P1–P8 principles](../en/concepts/principles.md).

## Phase decisions (must hold)

- [ ] Each phase declares **only** `input` (and optional `preprocessor`, `role`, `can_finish`). [P1, P5]
- [ ] No phase markdown enumerates output fields. [P5, P8]
- [ ] No phase markdown describes Control IR format. [P8]
- [ ] No phase markdown names the next phase. The Skill graph owns transitions. [P1]
- [ ] Phase instructions describe **what** to do, **when** to choose which candidate, and domain rules — nothing else.

## Skill decisions (must hold)

- [ ] `skill.md` declares `entry`, `graph`, `final_output`. [P2, P6]
- [ ] `final_output` is a single artifact type, not a union.
- [ ] `graph` uses `end` as the terminal sentinel for any phase whose `can_finish: true`.
- [ ] If the skill has multiple paths, every leaf path eventually reaches `end`.

## Artifact decisions (must hold)

- [ ] Each artifact has a clear, single-purpose schema.
- [ ] Avoid kitchen-sink artifacts that carry unrelated fields "just in case."
- [ ] Required fields are minimal — anything that *could* be optional, mark optional.
- [ ] Field names are lowercase_snake_case.

## OS-agnosticism red flags

If you find yourself wanting any of these, stop and reconsider — they signal a P7 violation in the skill design:

- [ ] A field called `quality_notes`, `revision_reason`, or other meta-feedback baked into a domain artifact. → use a separate review artifact instead.
- [ ] A `decision` field with values like `revise`, `redo`, `improve`. → use `continue` and let the graph route to a revise phase.
- [ ] Phase instructions that say "the next phase will need X" → just describe X in this phase's output (which is the next phase's input).

## Python preprocessor checklist

If a phase uses a `python` preprocessor step:

- [ ] The `.py` file lives in the skill directory (relative path only — no `..`, no absolute paths).
- [ ] Mode is `pure` unless trusted access is genuinely required.
- [ ] `output_schema` is declared explicitly (the LLM-visible enrichment shape).
- [ ] The skill's `permissions.python` lists the same module/function pair as the preprocessor step.
- [ ] In pure mode: no `open`, `eval`, `exec`, `__import__`, `subprocess`, or imports outside the allowlist.
- [ ] If the function is non-trivial (>30 LOC), break it up — preprocessor functions should be small and obvious.

## Eval-friendliness

- [ ] Each phase has a clear input/output relationship that an LLM judge can verify against criteria.
- [ ] Avoid criteria that would be "vacuously true" under any reasonable phase output (e.g. "char_count is correct" for a Python preprocessor that always computes it correctly).

## Final lint

After writing files:

- [ ] Run `reyn lint <skill_name>` and fix all errors.
- [ ] Treat warnings as advisory but read them.

## When you've finished

If every box is checked, the skill is ready for first-run testing. The very first run on a sample input usually reveals one or two missed details — that's normal. Use [skill_improver](../en/reference/stdlib/skill_improver.md) for systematic refinement.
