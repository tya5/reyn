---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation and Observability

Two questions a serious agent system has to answer: *does it work?* (evaluation) and *why is it doing what it's doing?* (observability). reyn answers both through the same channel — events — plus a stdlib skill (`eval`) that uses events to grade rubric criteria.

## How reyn handles it

### Events: the runtime's diary

Every state change emits a structured event. The full set is captured in JSONL at `.reyn/events/<run_id>.jsonl`:

- **Lifecycle.** `workflow_started`, `phase_started`, `phase_completed`, `workflow_finished`, `phase_failed`, `loop_limit_exceeded`.
- **LLM and context.** `context_built`, `llm_called` (with token counts and latency), `validation_error`, `normalization_error`.
- **Control IR.** One event per op kind, plus `permission_denied`.
- **User interaction.** `user_intervention_requested`, `user_intervention_received`, `chat_started`, `chat_stopped`.

There is no separate logger, tracer, or telemetry hook. The same channel powers live console output, replay, and eval analytics.

### Replay

```bash
reyn events .reyn/events/<run_id>.jsonl
reyn events <log> --conversation       # show LLM context + raw response per turn
reyn events <log> --filter validation_error --skip context_built
```

Replay does not re-invoke the LLM. It re-renders the saved log to the console with the same formatting as a live run. The log alone is sufficient for post-hoc analysis.

### Eval — phase-keyed rubric grading

The `eval` stdlib skill judges a target skill's output against per-phase criteria using `judge_phase` fanned out over the rubric (one item per criterion). The result is a structured report: per-case pass/fail, weakest phase, overall score, token usage, cost.

```bash
reyn eval reyn/local/my_skill/eval.md
```

Two related stdlib skills:

- **`eval_builder`** generates a rubric draft from a skill description.
- **`judge_phase`** is the per-criterion judge that `eval` orchestrates.

The phase-keyed structure matters: a "the summary is friendly" criterion is graded against the *summary* phase's output, not the whole run. This makes it possible to find the *responsible* phase for a regression instead of just noting that the final output is worse.

### Cost and token observability

Every `llm_called` event carries input/output token counts. `reyn run` and `reyn eval` print per-run totals (tokens + USD cost) at the end; eval reports persist them per case. This makes "did we get cheaper?" measurable across runs.

## Where it's still thin

The grading judge is itself an LLM, so eval scores carry whatever bias and variance a judge inherits. Mitigations include phrasing criteria that are testable from output alone (numerical thresholds, structural checks) and using stronger models for the judge than the system under test. The runtime does not currently support deterministic check-only criteria (e.g. "the output has exactly 3 bullets") as a separate code path — they're written as criteria the judge evaluates.

There is no built-in cost dashboard or longitudinal eval trend view; consumers parse the per-run reports themselves. The data is structured enough to plug into existing observability tooling.

## See also

- [events.md](../events.md) — concept
- [Reference: events](../../reference/runtime/events.md)
- [Reference: stdlib/eval](../../reference/stdlib/eval.md)
- [Reference: stdlib/eval_builder](../../reference/stdlib/eval_builder.md)
- [Reference: cli/eval](../../reference/cli/eval.md)
- [How-to: debug with events](../../guide/for-skill-authors/debug-with-events.md)
- [reliability-engineering.md](reliability-engineering.md) — events for failure
- [product-think.md](product-think.md) — surfacing observability through CLI affordances
