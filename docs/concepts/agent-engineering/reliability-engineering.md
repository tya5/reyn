---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

> **Status: partially stale.** This page predates the deleted phase-graph skill
> engine. The LLM-call-timeout and LLM-router-resilience sections below are
> current and unaffected — confirmed live in `docs/reference/config/reyn-yaml.md`.
> The "Python preprocessor timeout" section describes a step kind
> (`python` preprocessor) that no longer exists in the current pipeline DSL
> (confirmed via `docs/reference/runtime/pipeline-dsl.md` — no such step kind).
> A full rewrite covering the current crash-recovery/WAL substrate is tracked
> as a follow-up; in the meantime see [Time-travel](../runtime/time-travel.md)
> and [Events](../runtime/events.md) for the current reliability story.

Making agents recover from failure: schema validation, re-prompt on rejection, loop bounds, per-step timeout, and retry policies. The bar is "the system stays in a defined state even when the LLM gets it wrong."

## How reyn handles it

### LLM call timeout and transient-error retry

Each LLM HTTP call carries a per-call timeout (`limits.llm.timeout`, default `60`s) passed through to LiteLLM, plus LiteLLM's built-in exponential-backoff retry (`limits.llm.max_retries`, default `3`) for transient failures (`429`, `5xx`, network resets). Application-level rejection (validation, normalization) is handled separately by the re-prompt loop above — these are different failure modes and don't share a budget.

### LLM router resilience (`llm.router.*`)

An opt-in `litellm.Router` slot-in for provider-level resilience. Default OFF (`llm.router.use: false`) — with the switch off the call path is the direct `litellm.acompletion`, byte-identical to behaviour before this feature existed. When `use: true`, the Router owns infra-exception retry, `Retry-After` header handling, per-deployment cooldown, and a cross-model fallback chain; Reyn does not re-implement any of these.

**Retry-After aware retry.** `llm.router.num_retries` (default `3`) caps infra retries (`429`, `5xx`, network resets). Unlike a plain exponential backoff, the Router natively honours provider `Retry-After` response headers, so retry timing respects rate-limit windows rather than a fixed backoff schedule.

**Cross-model fallback chain.** `llm.router.fallbacks` maps each primary deployment to an ordered list of fallback models. On primary failure (after retries exhaust) the Router tries each fallback in order. `llm.router.cooldown_time` + `allowed_fails` cools a deployment after repeated failures so it is bypassed for subsequent calls until recovery.

**Cost accuracy on fallback.** The actual responding model is recorded from `response.model` so cost attribution reflects which deployment served the call, not the originally requested model.

**Replay compatibility.** The Router routes through the same `litellm.acompletion` chokepoint that LLMReplay monkeypatches — a realized fallback still exercises the replay machinery unchanged. The Router is cached per event loop with a `(model, config-fingerprint)` key so a changed `llm.router.*` rebuilds the instance rather than silently reusing a stale one.

See [Config: llm block](../../reference/config/reyn-yaml.md#llm-block) for the full field reference.

### Python preprocessor timeout

Per `python` preprocessor step, a wall-clock `timeout` (default `30`s) is enforced via subprocess. On timeout the parent SIGKILLs the child and the step fails — the failure surfaces to the LLM as a step result it can react to. The timeout protects against accidentally compute-heavy preprocessor functions (regex catastrophic backtracking, infinite loops in user code).

## Where it's still thin

A few reliability primitives are intentionally simple today and on the roadmap to deepen:

**Checkpoint/resume is now implemented, WAL-backed rather than audit-event-backed.** Crash recovery reconstructs agent state from the WAL (`.reyn/state/wal.jsonl`) plus seq-keyed snapshots, and user-initiated rewind (`/rewind`) forks history at a past checkpoint the same way. See [Time-travel](../runtime/time-travel.md) for the mechanism — this superseded the "not built yet" note that used to be here.

**Idempotency is the workflow author's responsibility.** If a workflow writes a file via Control IR, re-running the same step on retry will write again. Deterministic preprocessing helps, but workflows with externally-visible side effects need to think about idempotency themselves.

## See also

- [Reference: events](../../reference/runtime/events.md) — full event taxonomy
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `limits` block · [llm.router.*](../../reference/config/reyn-yaml.md#llm-block)

- [tool-contract-design.md](tool-contract-design.md) — what gets validated
