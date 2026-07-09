---
type: concept
topic: architecture
audience: [human, agent]
---

# Reliability Engineering

Making agents recover from failure: schema validation, re-prompt on rejection, loop bounds, per-step timeout, and (longer-term) retry policies and checkpoint/resume. The bar is "the system stays in a defined state even when the LLM gets it wrong."

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

**No checkpoint/resume.** Because every state change is an event (P3), the *information* needed for resume is already in the log; the *machinery* to reload at event N and continue isn't built. Adding it doesn't require new event types — just a runtime mode that replays events as state-restore rather than just rendering.

**Idempotency is the workflow author's responsibility.** If a workflow writes a file via Control IR, re-running the same step on retry will write again. Deterministic preprocessing helps, but workflows with externally-visible side effects need to think about idempotency themselves.

## See also

- [Reference: events](../../reference/runtime/events.md) — full event taxonomy
- [Reference: reyn.yaml](../../reference/config/reyn-yaml.md) — `limits` block · [llm.router.*](../../reference/config/reyn-yaml.md#llm-block)

- [tool-contract-design.md](tool-contract-design.md) — what gets validated
