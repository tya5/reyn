---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

Making agents recover from failure: schema validation, re-prompt on rejection, loop bounds, per-step timeout, and (longer-term) retry policies and checkpoint/resume. The bar is "the system stays in a defined state even when the LLM gets it wrong."

## How reyn handles it

### Validation + re-prompt

Every LLM output passes through a fixed validation pipeline before any side effect runs:

1. **Normalize.** Try to parse the response as the contract JSON. Failure → `normalization_error` event, re-prompt.
2. **Validate the control envelope.** Check `type`/`decision`/`next_phase` consistency. Failure → `validation_error`, re-prompt.
3. **Validate the artifact.** Check the artifact against the chosen target's input schema. Failure → `validation_error`, re-prompt.
4. **Validate the Control IR.** Check op shapes and permissions. Failure → `control_ir_validation_error`, re-prompt.

The OS never silently fixes up bad output. After a configurable number of failed re-prompts the run aborts; the failure is visible in the event log.

Each retry emits a `phase_retry` event. The retry counter is per phase visit, so a phase that needs three tries is a normal occurrence — the reliability problem is unbounded retries, which the OS prevents.

### Loop bounds

`max_phase_visits` (default `25`, `0` = unlimited) caps how many times any single phase can be revisited within one run. When the cap is hit, the OS emits `loop_limit_exceeded` and ends the run with status `loop_limit_exceeded` rather than spinning forever. The cap is configurable per-run via `--max-phase-visits` and at the project level via `reyn.yaml`.

This protects against:

- A revision loop that the LLM can't satisfy (a criterion is unreachable).
- A graph that lets the LLM keep choosing the same branch.
- A subtle bug where two phases ping-pong indefinitely.

### Python preprocessor timeout

Per `python` preprocessor step, a wall-clock `timeout` (default `30`s) is enforced via subprocess. On timeout the parent SIGKILLs the child and the step fails — the failure surfaces to the LLM as a step result it can react to. The timeout protects against accidentally compute-heavy preprocessor functions (regex catastrophic backtracking, infinite loops in user code).

### Failure visibility

Every reliability event lands in the JSONL log:

| Event | What happened |
|-------|---------------|
| `validation_error` | OS rejected an artifact / control envelope |
| `normalization_error` | OS couldn't parse the LLM response at all |
| `control_ir_validation_error` | OS rejected an op |
| `phase_retry` | A retry of a rejected output |
| `loop_limit_exceeded` | The visit cap was hit |
| `phase_failed` | A phase raised an unrecoverable error |

`reyn events <log> --filter validation_error --filter normalization_error` jumps straight to the trouble.

## Where it's still thin

A few reliability primitives are intentionally simple today and on the roadmap to deepen:

**Retry policy is "re-prompt up to N times."** Each retry uses the same prompt with the validation error injected as feedback. There is no exponential backoff, no jitter, no per-failure-kind strategy. For LLM rejections this is usually adequate; for transient API errors it is not (the OS surfaces the error rather than retrying with backoff).

**No global wall-clock timeout.** Individual `python` steps have timeouts; an LLM call that hangs at the provider layer will hang the run. In practice the LLM provider's timeout is the floor.

**No checkpoint/resume.** Because every state change is an event (P3), the *information* needed for resume is already in the log; the *machinery* to reload at event N and continue isn't built. Adding it doesn't require new event types — just a runtime mode that replays events as state-restore rather than just rendering.

**Idempotency is the skill author's responsibility.** If a phase writes a file via Control IR, re-entering the phase on retry will write again. The preprocessor + Control IR distinction helps (preprocessors are deterministic), but skills with externally-visible side effects need to think about idempotency themselves.

## See also

- [Reference: events](../../reference/runtime/events.md) — full event taxonomy
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [Reference: common-flags](../../reference/cli/common-flags.md) — `--max-phase-visits`
- [How-to: debug with events](../../how-to/debug-with-events.md)
- [evaluation-and-observability.md](evaluation-and-observability.md) — measuring failure rates
- [tool-contract-design.md](tool-contract-design.md) — what gets validated
