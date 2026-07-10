---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

Making agents recover from failure: bounded loops that stop gracefully rather than hard-failing, timeout + retry, and crash recovery that survives a process death mid-run. The bar is "the system stays in a defined state — and reports what it accomplished — even when something goes wrong."

## How reyn handles it

### Crash recovery — WAL-backed, not audit-event-backed

Crash recovery reconstructs agent state from the WAL (`.reyn/state/wal.jsonl`) plus seq-keyed snapshots — a separate substrate from the P6 audit-event log, which is the per-run trace, not the recovery source. User-initiated rewind (`/rewind`) uses the same WAL-backed mechanism to fork history at a past checkpoint. See [Time-travel](../runtime/time-travel.md) for the full mechanism.

### Bounded loops with graceful force-close

Seven loop / timeout / budget checkpoints share one function, `handle_limit_exceeded` — callers only decide what limit fired; the checkpoint itself owns mode dispatch, operator-bus interaction, extension bookkeeping, and audit-event emission. Three on-limit modes (`safety.on_limit.mode`) apply uniformly across every checkpoint: `interactive` (ask the operator), `auto_extend` (extend a bounded number of times, then abort), `unattended` (abort immediately, never ask).

Critically, none of these paths hard-stop silently: on a denied limit, the LLM gets one final tool-less turn to summarise what it accomplished, delivered as an agent message rather than a vanished process. Every deny path also emits a `limit_denied` P6 audit-event — the operator can always see which limit fired and why.

### LLM call timeout and transient-error retry

Each LLM HTTP call carries a per-call timeout (`limits.llm.timeout`, default `60`s) passed through to LiteLLM, plus LiteLLM's built-in exponential-backoff retry (`limits.llm.max_retries`, default `3`) for transient failures (`429`, `5xx`, network resets).

### LLM router resilience (`llm.router.*`)

An opt-in `litellm.Router` slot-in for provider-level resilience. Default OFF (`llm.router.use: false`) — with the switch off the call path is the direct `litellm.acompletion`, byte-identical to behaviour before this feature existed. When `use: true`, the Router owns infra-exception retry, `Retry-After` header handling, per-deployment cooldown, and a cross-model fallback chain; reyn does not re-implement any of these.

**Retry-After aware retry.** `llm.router.num_retries` (default `3`) caps infra retries (`429`, `5xx`, network resets). Unlike a plain exponential backoff, the Router natively honours provider `Retry-After` response headers, so retry timing respects rate-limit windows rather than a fixed backoff schedule.

**Cross-model fallback chain.** `llm.router.fallbacks` maps each primary deployment to an ordered list of fallback models. On primary failure (after retries exhaust) the Router tries each fallback in order. `llm.router.cooldown_time` + `allowed_fails` cools a deployment after repeated failures so it is bypassed for subsequent calls until recovery.

**Cost accuracy on fallback.** The actual responding model is recorded from `response.model` so cost attribution reflects which deployment served the call, not the originally requested model.

**Replay compatibility.** The Router routes through the same `litellm.acompletion` chokepoint that LLMReplay monkeypatches — a realized fallback still exercises the replay machinery unchanged.

See [Config: llm block](../../reference/config/reyn-yaml.md#llm-block) for the full field reference.

## Where it's still thin

**Idempotency is the caller's responsibility.** If an agent writes a file via a Control IR op, re-running the same op on retry writes again — the OS doesn't make ops idempotent on your behalf. Callers with externally-visible side effects need to think about idempotency themselves.

## See also

- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the Reliability row, grounded across all 7 feature families
- [Time-travel](../runtime/time-travel.md) — the WAL-backed crash-recovery and rewind mechanism in full
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `safety` block · [llm.router.*](../../reference/config/reyn-yaml.md#llm-block)
- [tool-contract-design.md](tool-contract-design.md) — what gets validated before an op runs
