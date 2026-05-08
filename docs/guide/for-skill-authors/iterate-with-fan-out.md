---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md]
---

# Fan out a sub-skill over a list

**Goal:** Run the same sub-skill once per item in a list and collect the results, deterministically, before the LLM sees them.

## When to use

- You have N inputs and want N independent decisions (e.g., judge N criteria, summarize N documents).
- Each item is independent — no item needs another item's output.
- You want a stable, replayable pipeline rather than letting the LLM orchestrate the loop.

## Pattern

```yaml
---
type: phase
name: judge_all_criteria
input: phase_eval_request_batch
preprocessor:
  - iterate:
      over: phase_eval_requests
      apply:
        run_skill:
          skill: judge_phase
          input:
            type: phase_eval_request
            data: ${item}
      into: phase_judgments
      on_error: fail
---

Aggregate `phase_judgments` into an overall verdict.
```

What happens:

1. The OS reads the array at `phase_eval_requests` from the phase input.
2. For each `${item}`, it invokes `judge_phase` to completion and collects the `final_output`.
3. The collected list lands at `input.phase_judgments`.

## `on_error`

| Value | Behavior |
|-------|----------|
| `fail` (default) | The first sub-skill failure stops the iteration and bubbles up |
| `skip` | Failed items are dropped from the result list; the iteration continues |

Use `skip` when partial results are still useful (eval reports, batch summaries) and `fail` when one bad item should abort.

## What you can put in `apply`

MVP supports `run_skill` only. If you need to do something else per item, build a sub-skill that does that thing and iterate on it.

## A real example: `eval`

The stdlib `eval` skill iterates `judge_phase` over per-criterion requests:

```yaml
preprocessor:
  - iterate:
      over: phase_eval_requests
      apply:
        run_skill:
          skill: judge_phase
          input: { type: phase_eval_request, data: ${item} }
      into: phase_judgments
```

The judging phase then reads `phase_judgments` and aggregates.

## See also

- [Reference: preprocessor](../../reference/dsl/preprocessor.md) — `iterate` step
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md)
- [Reference: stdlib/eval](../../reference/stdlib/eval.md)
