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

### Loop bounds and phase budgets

Two complementary bounds apply per phase, both configured under `limits.phase` in `reyn.yaml`:

- **`max_visits`** (default `25`, `0` = unlimited) caps how many times any single phase can be revisited within one run. On exceed the OS emits `loop_limit_exceeded` and ends the run with status `loop_limit_exceeded`.
- **`max_wall_seconds`** (default `0` = unlimited) sets a wall-clock budget per phase visit. The check is *soft*: the OS evaluates elapsed time at retry/turn boundaries rather than canceling mid-call. On exceed the OS emits `phase_budget_exceeded` and ends the run with status `phase_budget_exceeded`. Workspace state stays consistent because no in-flight work is killed.

Both can be overridden per-run via `--max-phase-visits` and `--phase-budget`.

This protects against:

- A revision loop that the LLM can't satisfy (a criterion is unreachable).
- A graph that lets the LLM keep choosing the same branch.
- A subtle bug where two phases ping-pong indefinitely.
- A phase that *does* terminate but takes too long to be useful (slow LLM, runaway preprocessor chain).

### LLM call timeout and transient-error retry

Each LLM HTTP call carries a per-call timeout (`limits.llm.timeout`, default `60`s) passed through to LiteLLM, plus LiteLLM's built-in exponential-backoff retry (`limits.llm.max_retries`, default `3`) for transient failures (`429`, `5xx`, network resets). Application-level rejection (validation, normalization) is handled separately by the re-prompt loop above — these are different failure modes and don't share a budget.

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
| `phase_budget_exceeded` | The wall-clock budget for the current phase was hit |
| `phase_failed` | A phase raised an unrecoverable error |

`reyn events <log> --filter validation_error --filter normalization_error` jumps straight to the trouble.

## Where it's still thin

A few reliability primitives are intentionally simple today and on the roadmap to deepen:

**Retry policy splits cleanly but isn't deep.** Application-level rejections re-prompt up to `max_phase_retries` (default `2`) with the validation error injected as feedback — no jitter, no per-failure-kind strategy. Transient HTTP errors get LiteLLM's built-in exponential backoff via `limits.llm.max_retries`. The two paths don't share state, which is the right shape, but neither path lets the user customize backoff or per-error policy yet.

**Phase budget is soft, not a hard cancel.** `limits.phase.max_wall_seconds` checks at retry/turn boundaries — a single very long LLM call or preprocessor step can overshoot the budget by one operation. This trades "hard guarantees" for "consistent workspace state," and is the right default for most workflows; mid-call cancellation is on the roadmap as an opt-in mode.

**No checkpoint/resume.** Because every state change is an event (P3), the *information* needed for resume is already in the log; the *machinery* to reload at event N and continue isn't built. Adding it doesn't require new event types — just a runtime mode that replays events as state-restore rather than just rendering.

**Idempotency is the skill author's responsibility.** If a phase writes a file via Control IR, re-entering the phase on retry will write again. The preprocessor + Control IR distinction helps (preprocessors are deterministic), but skills with externally-visible side effects need to think about idempotency themselves.

## See also

- [Reference: events](../../reference/runtime/events.md) — full event taxonomy
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [Reference: common-flags](../../reference/cli/common-flags.md) — `--max-phase-visits`, `--phase-budget`, `--llm-timeout`, `--llm-max-retries`
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `limits` block
- [How-to: debug with events](../../how-to/debug-with-events.md)
- [evaluation-and-observability.md](evaluation-and-observability.md) — measuring failure rates
- [tool-contract-design.md](tool-contract-design.md) — what gets validated
