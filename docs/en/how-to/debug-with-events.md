---
type: how-to
topic: runtime
audience: [human]
applies_to: [.reyn/events/]
---

# Debug a run with events

**Goal:** Find why a run produced unexpected output (or failed), using only the saved event log.

## Find the run

Every `reyn run` ends with:

```
events saved → .reyn/events/<run_id>.jsonl
```

Replay it:

```bash
reyn events .reyn/events/<run_id>.jsonl
```

The output looks like a live run, formatted the same way.

## Common debug questions

### "What did the LLM actually see / say?"

```bash
reyn events <log> --conversation
```

Shows context frames sent to the LLM and the raw responses, in order. This is the closest thing reyn has to a debugger.

### "Where did the OS reject the LLM's output?"

```bash
reyn events <log> --filter validation_error --filter normalization_error
```

`validation_error` = output didn't match the chosen target's schema.
`normalization_error` = output couldn't even be parsed as the contract JSON.

### "Why did this phase get hit so many times?"

```bash
reyn events <log> --filter phase_started --filter phase_completed
```

Each `phase_started` increments visit count. If you see the same phase repeatedly, look at its `phase_completed → next_phase` to see what loop it's in.

### "Why was a Control IR op denied?"

```bash
reyn events <log> --filter permission_denied
```

The payload includes the op and the missing permission key.

### "Did the run hit the visit cap?"

```bash
reyn events <log> --filter loop_limit_exceeded
```

If yes: either the phase is genuinely stuck (fix the prompt or graph) or the cap is too low (raise `--max-phase-visits`).

## Event filtering

`--filter` and `--skip` each take an event kind and are repeatable:

```bash
reyn events <log> --filter llm_called --skip context_built
```

Without filters, every event is printed.

## Replay does not call the LLM

Replay is purely a render of saved events. The LLM is not re-invoked. If you change a prompt and want to see the new behavior, rerun with `reyn run` — replay still shows the old run.

## See also

- [Reference: events](../reference/runtime/events.md) — full event taxonomy
- [Concepts: events](../concepts/events.md)
- [Reference: run](../reference/cli/run.md) — `--events` flag
