---
type: concept
topic: architecture
audience: [human, agent]
---

# Observability

Leaving a trace sufficient to inspect and reconstruct what happened, after the fact and live. The bar is "when something looks wrong, there's a log to point at — not a debugger session to reconstruct one."

## How reyn handles it

### The P6 audit-event log

Every state change reyn's own OS causes emits an audit-event to a JSONL stream (`.reyn/events/<run_id>.jsonl`) — one channel powering live debug output, `reyn events` replay, and eval analytics. There is no separate logger, tracer, or telemetry hook bolted on afterward. See [Events](../runtime/events.md) for the full model, including the important distinction this lens has to keep sharp: audit-event (the observability trace) is a different thing from WAL-event (the crash-recovery/time-travel substrate) and hook-event (an external reactivity trigger) — conflating them is a real category error, not a stylistic nitpick (`CLAUDE.md`'s Constitution section names all three explicitly).

### `chain_id` — tracing a request across hops

A single top-level user submission mints a `chain_id` (uuid4 hex) that propagates unchanged across every agent-to-agent hop it produces. Cross-agent reconstruction of one logical request is `grep <chain_id>` over each agent's own `events.jsonl` — no centralized trace collector is needed because the identifier travels with the message itself.

### `reyn events` replay

Replays a saved audit-event log to the console with the same rendering as a live run, without re-invoking the LLM — the log is the debugging tool, not a supplement to one. `--filter TYPE` narrows to one event kind (e.g. `--filter permission_denied` to jump straight to a denied op).

### Live audit chips (inline CUI)

The inline CUI's status-chip bar (Agents / Cost / Model / Tools / MCP / Skills / Hooks / Pipes / Cron / Tasks) is this same audit trace surfaced live, inline, rather than only available via after-the-fact replay — the operator sees the same state the P6 log records, in real time.

## Where it's still thin

Aggregation across runs is thin: there is no cross-run trend view or dashboard built on top of the audit-event log — each run's `.jsonl` file is a complete, self-contained record, but rolling them up into fleet-level observability is left to the operator's own tooling (the data is structured enough to feed into one). Payload-level trace inspection for LLM calls specifically (not just event kinds) is a separate, complementary surface — see [`docs/reference/dogfood-tracing.md`](../../reference/dogfood-tracing.md).

## See also

- [Concepts: events](../runtime/events.md) — the audit-event/WAL-event/hook-event model in full
- [Reference: events](../../reference/runtime/events.md) — the full audit-event taxonomy
- [Reference: `reyn events`](../../reference/cli/events.md) — replay CLI
- [reliability-engineering.md](reliability-engineering.md) — the WAL-backed substrate this lens must not be confused with
