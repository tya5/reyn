---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [phases/*.md]
---

# `phase.md` frontmatter

Each phase lives in `phases/<phase_name>.md` under its skill directory. The YAML frontmatter declares only what the phase consumes — never what it produces, never which phase comes next ([P1, P5](../../concepts/principles.md)).

## Schema

```yaml
---
type: phase                    # always "phase"
name: <phase_name>             # must match the filename (without .md)
input: <artifact_type>         # required; what this phase consumes
role: <short_label>            # optional; one-word role for events
can_finish: true               # optional; allow terminating from here (default: false)
allowed_ops: [file, ask_user]  # optional; Control IR op kinds this phase may use
                                # (default: ["file", "ask_user"]; [] means no ops)
preprocessor:                  # optional; deterministic pre-LLM steps
  - run_skill:
      skill: recall_memory
      input: { type: ..., data: { ... } }
      into: relevant_memories
  - python:
      module: stats
      function: compute
      mode: pure                # pure | trusted
      output_schema: { ... }
---
```

## Required fields

- **`type`** — must be `phase`.
- **`name`** — string, identifies this phase in the skill's `graph`. Must match the filename.
- **`input`** — artifact type the phase reads. Either a single artifact name or a union (`user_message | topic_input`).

## Optional fields

- **`role`** — short label for event payload (e.g. `planner`, `reviewer`).
- **`can_finish`** — when `true`, the LLM may emit `decision="finish"` from this phase. The OS validates the final artifact against the skill's `final_output_schema`.
- **`preprocessor`** — chain of deterministic steps that run before the LLM call. See `reference/dsl/preprocessor.md` (Phase 2).
- **`allowed_ops`** — list of Control IR op kinds this phase may emit (e.g. `[file, lint]`). The OS filters the `available_control_ops` advertised to the LLM down to this set, *and* rejects any out-of-set op the LLM emits anyway with `control_ir_skipped: not_allowed_in_phase`. Default: `["file", "ask_user"]` — file I/O plus user clarification, the common case. An explicit empty list (`[]`) means "no ops" (use this for pure routing/judging phases). The narrower the list, the less context spent on op descriptions and the less room the LLM has to drift outside the phase's intent.

## What MUST NOT appear

- Output schema of any kind. Output is determined by the next phase's input or the skill's `final_output` ([P5](../../concepts/principles.md#p5-no-output-schema-in-phase)).
- The next phase name. The Skill graph owns transitions ([P1](../../concepts/principles.md#p1-phase-is-stateless-and-reusable)).
- Control IR format descriptions. The OS injects available ops into the context frame ([P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)).

## Body

The markdown body is the phase's instructions to the LLM. Cover:

- **What** to analyze, generate, or decide
- **When** to choose which next-phase candidate
- Domain-specific rules, examples, and edge cases

Avoid restating schemas, listing field names, or describing Control IR — these are runtime-injected.

## Example

```yaml
---
type: phase
name: outline
input: topic_input
role: planner
---

Produce three bullet points that capture the most important angles of
the topic. Each bullet should be a complete sentence — the next phase
will expand each into a paragraph, so vague bullets produce vague
paragraphs.

Avoid: meta-commentary, scope hedging, more than three bullets.
```

## See also

- [skill-md.md](skill-md.md) — Skill frontmatter
- `reference/dsl/preprocessor.md` — preprocessor steps (Phase 2)
- [Concepts: principles P1, P5, P8](../../concepts/principles.md)
