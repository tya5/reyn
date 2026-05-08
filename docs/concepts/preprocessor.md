---
type: concept
topic: architecture
audience: [human, agent]
---

# Preprocessor

A **preprocessor** is a chain of deterministic steps that runs before the LLM
is called in a phase. Each step enriches the input artifact — its result is
placed at a key named by `into`. By the time the LLM receives the context
frame, the input artifact already contains computed facts the LLM can cite
rather than guess.

It mirrors the [postprocessor](postprocessor.md) structurally — same step
types, same `on_error` semantics, same permission gate. The only difference
is **fire position**: preprocessor fires at phase entry, postprocessor fires
at skill finish.

## Why

### Deterministic work does not need an LLM

LLMs are unreliable at counting characters, summing rows, or measuring token
length — tasks that Python handles precisely in microseconds. Pre-computing
facts the LLM would otherwise estimate (or hallucinate) is a foundational
agent-engineering pattern. Preprocessor is where that computation lives in
Reyn.

This is the **deterministic split** principle: if an output is a pure function
of the input, derive it deterministically rather than asking the LLM to
reproduce it. See
[system-design.md](agent-engineering/system-design.md) for the broader
framing. This principle aligns with
[P3](principles.md#p3-os-controls-execution) — the OS controls execution —
and [P5](principles.md#p5-workspace-is-the-single-source-of-truth) — all data
flows through the workspace, not through the LLM's reasoning chain.

### Reduced token cost and improved correctness

Every fact the preprocessor computes is one fewer inference the LLM needs to
make. This narrows the LLM's responsibility to judgment, synthesis, and
generation — the things LLMs are good at. A phase that arrives at the LLM
with `stats.word_count = 847` already in the artifact produces more accurate
output than one that asks the LLM to estimate word count inline.

### Phase composability

A phase that reads `data.stats` doesn't care how `stats` got there. The same
phase can be re-targeted at different skills by swapping what the preprocessor
feeds into it, without changing the phase's instructions or schema. This is
the phase reuse guarantee from [P1](principles.md#p1-phase-is-stateless-and-reusable).

## Step kinds at a glance

| Step type | What it is for |
|-----------|----------------|
| `run_skill` | Invoke a sub-skill, store its final output at `into` |
| `iterate` | Fan a sub-step out over a list, collect results into `into` |
| `validate` | Run a JSON-Schema check; surface findings so the LLM can judge them |
| `lint_plan` | Run deterministic structural checks (cycles, coverage) on a plan artifact |
| `python` | Call a user-supplied Python function in sandboxed `pure` or `trusted` mode |

All steps share two invariants: the result is placed at `into`, and steps run
in declaration order — each step can read what earlier steps produced.

Full syntax for each step type is in
[reference/dsl/preprocessor.md](../reference/dsl/preprocessor.md).

## When to use a preprocessor vs letting the LLM handle it

| Situation | Right home |
|-----------|------------|
| Counting, measuring, summing | Preprocessor (`python`) |
| Calling a known sub-skill before the main phase | Preprocessor (`run_skill`) |
| Fan-out over a list | Preprocessor (`iterate`) |
| "Check this before deciding" | Preprocessor (`validate`) — gives the LLM the findings, then it judges |
| Structural sanity-check on a plan | Preprocessor (`lint_plan`) |
| Open-ended judgment, synthesis, or generation | LLM |

The guiding question: "Is the output of this step a pure function of the
input?" If yes, it belongs in the preprocessor.

## Symmetry with postprocessor

| | Preprocessor | Postprocessor |
|---|---|---|
| Fires at | Phase entry | Skill finish |
| Input source | Upstream phase's output | LLM's finish artifact |
| Output target | Phase's `input_schema` (enriched) | Postprocessor's `output_schema` |
| Step types | `run_skill` / `iterate` / `validate` / `lint_plan` / `python` | Identical |
| `on_error` policy | `fail` / `skip` / `empty` per step | Identical |
| Permission gate | `skill.permissions` | Identical |

The runner shares logic between both — differences are which artifact flows in,
which schema validates the output, and the fire site.

## Worked example: `word_stats_demo`

The `word_stats_demo` stdlib skill is the simplest canonical example. Its
`review` phase declares a single `python` preprocessor step:

```yaml
preprocessor:
  - type: python
    module: ./stats.py
    function: compute_text_stats
    into: data.stats
    output_schema:
      type: object
      properties:
        char_count:        {type: integer, minimum: 0}
        word_count:        {type: integer, minimum: 0}
        line_count:        {type: integer, minimum: 0}
        longest_line_chars: {type: integer, minimum: 0}
        estimated_tokens:  {type: integer, minimum: 1}
      required: [char_count, word_count, line_count, longest_line_chars, estimated_tokens]
```

What it gives the LLM: `input_artifact.data.stats` is already populated with
exact counts before the LLM call. The phase instructions tell the LLM to "cite
at least one stat verbatim" — which is reliable precisely because the numbers
come from Python, not from the LLM's own estimation.

## Failure semantics

A preprocessor step can declare `on_error: fail | skip | empty`:

- **`fail`** (default): step failure raises and aborts the phase.
- **`skip`**: failure is logged; subsequent steps continue.
- **`empty`**: failure produces an empty value at `into`; subsequent steps
  continue.

Default to `fail` for steps whose output is load-bearing. Use `skip` or
`empty` for enrichment that is useful but not required for the LLM to proceed.

## What phases must not do

Per [P8](principles.md#p8-phase-instructions-contain-only-domain-logic), phase
instructions must not describe how the preprocessor works or enumerate the
mechanics of Control IR. The instructions should refer to enriched fields by
name (`data.stats`) and explain what to do with them — not explain where they
came from.

## See also

- [reference/dsl/preprocessor.md](../reference/dsl/preprocessor.md) — full
  step syntax and options.
- [concepts/postprocessor.md](postprocessor.md) — symmetric postprocessor
  (fires at skill finish).
- [concepts/principles.md](principles.md) — P3 (OS controls execution), P5
  (Workspace is the single source of truth).
- [concepts/agent-engineering/system-design.md](agent-engineering/system-design.md)
  — deterministic split as a system-design principle.
