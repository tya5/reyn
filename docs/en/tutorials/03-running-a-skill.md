---
type: tutorial
topic: getting-started
audience: [human]
---

# 03 — Running a skill

You wrote `my_explainer` in tutorial 02. This tutorial covers the runtime side: input formats, common flags, and reading the event log.

## Three ways to feed input

### Natural language (auto-wrapped)

```bash
reyn run my_explainer "photosynthesis"
```

A bare string becomes `{"type": "user_message", "data": {"text": "photosynthesis"}}`. The skill's entry phase must accept `user_message` (or a union including it).

### JSON (used as-is)

```bash
reyn run my_explainer '{"type": "topic_input", "data": {"topic": "photosynthesis"}}'
```

The string must parse as a valid artifact: a top-level object with `type` and `data` keys.

### Stdin

```bash
echo "photosynthesis" | reyn run my_explainer
```

Same auto-wrapping as the positional form.

## Common flags

```bash
reyn run my_explainer "photosynthesis" \
  --model strong \
  --output-language en \
  --max-phase-visits 10 \
  --strict
```

- `--model strong` — pick a stronger model just for this run (overrides `reyn.yaml`).
- `--output-language en` — cue the LLM to reply in English regardless of the project default.
- `--max-phase-visits 10` — cap revisits to any single phase. `0` = unlimited.
- `--strict` — enforce required fields at every nesting depth (default: top level only).

The full list is on the [common-flags page](../reference/cli/common-flags.md).

## Watching what happened

Every run ends with:

```
events saved → .reyn/events/<run_id>.jsonl
```

To replay it:

```bash
reyn events .reyn/events/<run_id>.jsonl
```

To see the LLM conversation specifically:

```bash
reyn events .reyn/events/<run_id>.jsonl --conversation
```

To filter to specific event kinds:

```bash
reyn events .reyn/events/<run_id>.jsonl --filter validation_error
```

## When something looks wrong

1. Find the `phase_completed` event for the phase that produced the bad output.
2. Look at the matching `llm_called` event for what the model returned.
3. If you see `validation_error`, the model's output didn't fit the next target's schema — usually a phase-instruction issue.

The [debug-with-events](../how-to/debug-with-events.md) how-to walks through this flow.

## What you learned

- Inputs come from a positional argument (JSON or natural language) or stdin.
- Common flags override `reyn.yaml` for one run.
- Every run leaves a replayable JSONL log; the LLM is not re-invoked on replay.

## Next

- [Tutorial 04 — Writing an eval](04-writing-an-eval.md)
- [How-to: debug with events](../how-to/debug-with-events.md)
- [Reference: run](../reference/cli/run.md)
