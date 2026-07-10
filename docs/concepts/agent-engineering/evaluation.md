---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation

Scoring whether an agent's output is actually good — not just schema-valid. The bar is "the system can gate a critical decision on a judgment call, not just a type check."

## How reyn handles it

### `judge_output`

A typed Control IR op: resolves a `target` dot-path in the current workspace artifact, calls an LLM with a caller-supplied `rubric`, and returns a score (`0.0`–`1.0`) plus a `passed` flag against a `threshold` (default `0.8`).

```json
{
  "kind": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "Score 0.0-1.0: is the summary concise, accurate, and complete?",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

The OS never interprets the `rubric` content — it is the skill author's own evaluation criteria, routed to the LLM without inspection. `on_fail` (`"transition"` / `"abort"` / `"continue"`) is recorded in the result for the *caller* to act on; the op handler itself does not branch on it — resolving `on_fail` into an actual control-flow decision is the calling agent's own responsibility, not the OS's.

Every `judge_output` call emits a P6 audit-event (`tool_executed` with `op=judge_output`, `target`, `score`, `passed`, `threshold`, `reason`) — a scored decision is auditable the same way any other op is.

### `reyn eval`

A non-interactive CLI entry point for running an agent without a live approval prompt — permissions must already be pre-approved (in config or persisted from a prior interactive run) before the run starts. This is what makes `judge_output`-gated runs usable in CI: the scoring loop and the permission model are orthogonal, so an eval run's trust decisions are made once, up front, not re-litigated per invocation.

## Where it's still thin

This is one of the constitution's two declared honest thin areas (see `CLAUDE.md`'s Constitution section and [`docs/concepts/architecture/charter.md`](../architecture/charter.md), Evaluation row). `judge_output` is the entire evaluation surface — there is no rubric library, no multi-judge consensus/voting, no built-in eval-suite runner, and no aggregate scoring across a batch of runs. A skill author who wants any of that composes it themselves out of `judge_output` calls plus ordinary control flow; the OS provides the scoring primitive, not an evaluation framework built on top of it.

## See also

- [Reference: control-ir.md § `judge_output`](../../reference/runtime/control-ir.md)
- [Reference: events](../../reference/runtime/events.md) — audit-event taxonomy `judge_output` results land in
- [reliability-engineering.md](reliability-engineering.md) — what happens when validation, not judgment, is the bar
