---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation

Scoring whether an agent's output is actually good — not just schema-valid. The bar is "the system can gate a critical decision on a judgment call, not just a type check."

## How reyn handles it

### `agent` step + `schema`

There is no dedicated scorer op. Scoring an output against a rubric is ordinary pipeline composition: a pipeline `agent` step whose `schema:` names a small schema (e.g. `{score: number, reason: string}`), followed by a plain `transform` step that compares the parsed score against a threshold.

```yaml
pipeline: self_review
steps:
  - agent:
      prompt: "Self-review {ctx.draft} against your own checklist: ... Give a score in [0.0, 1.0] and a short reason."
      schema: Verdict
      output: verdict
  - transform: {value: "ctx.verdict.score >= 0.6", output: passed}
---
schema: Verdict
fields:
  score: {type: number}
  reason: {type: string}
```

The `schema:` is the OS's actual contribution here: it **constrains the agent's generation** (a `response_format` built from the schema, so the model answers in schema-conforming JSON rather than free text) and **validates the parsed result** afterwards (belt-and-suspenders — the provider constraint is not blindly trusted). The threshold comparison is a plain `if` (a `transform` step), not a bespoke op. Cost is tracked the same way every other `agent` step's cost is tracked — no separate cost path to wire.

The OS never interprets the checklist content — it is the calling agent's own evaluation criteria, part of the prompt it writes. **This is self-review, not objectivity**: the same agent (or model family) that produced the draft also writes the checklist and scores against it — useful for catching requirements the checklist names and the draft missed, but not an independent judge. Do not present it as one.

(A prior `judge_output` Control IR op offered a bespoke version of this — an LLM call with a `rubric` string, a `threshold` field, and an `on_fail` field the op itself never branched on. It was removed as a clean-break: the OS's actual contribution to it was a threshold comparison and an audit event, i.e. it was agent work — deciding what to draft, what to check for, and what threshold matters — wearing an OS-op costume. `schema` already did the same job — constrained generation + validation — properly, for any `agent` step, not just a scoring one.)

### `reyn run-once`

The non-interactive CLI entry point for running an agent without a live approval prompt (`reyn eval` was a phase-graph-era command; it was deleted alongside that engine — `reyn run-once` is its current, live counterpart). Permissions must already be pre-approved before the run starts — e.g. `--grant-file-write` grants a specific capability at invocation time rather than via an interactive prompt. This is what makes a self-review-gated pipeline usable in CI: the scoring loop and the permission model are orthogonal, so a non-interactive run's trust decisions are made once, up front, not re-litigated per invocation.

## Where it's still thin

This is one of the constitution's two declared honest thin areas (see `CLAUDE.md`'s Constitution section and [`docs/concepts/architecture/charter.md`](../architecture/charter.md), Evaluation row). An `agent` step + `schema` is the entire evaluation surface — there is no rubric library, no multi-judge consensus/voting, no built-in eval-suite runner, and no aggregate scoring across a batch of runs. An author who wants any of that composes it themselves out of `agent`+`schema` self-review steps plus ordinary pipeline control flow; the OS provides the typed-generation primitive, not an evaluation framework built on top of it.

## See also

- [Reference: pipeline-dsl.md § `AgentStep` / `schema`](../../reference/runtime/pipeline-dsl.md)
- [Reference: events](../../reference/runtime/events.md) — audit-event taxonomy an `agent` step's cost/completion events land in
- [reliability-engineering.md](reliability-engineering.md) — what happens when validation, not judgment, is the bar
