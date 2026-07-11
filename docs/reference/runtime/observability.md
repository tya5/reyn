---
type: reference
topic: runtime
audience: [human, agent]
---

# Observability — OpenTelemetry (OTLP) export

Reyn can export its P6 audit-event stream to an OpenTelemetry collector as OTLP
spans, metrics, and log records. The export is an **additive, opt-in,
fail-open downstream**: it is off unless an OTLP endpoint is configured, and it
never affects the session or any durable store.

## What it is — and is not

- **It is** a subscriber on the P6 [audit-event](../../concepts/runtime/events.md)
  log that maps each event to OTLP telemetry and emits it off-loop.
- **It is not** a recovery source. `.reyn/events` + the WAL remain the durable
  recovery/replay Source-of-Truth, unchanged. The exporter writes to neither.
- **It is not** a channel or client. Export only — Reyn does not receive OTLP.

## Opt-in — off by default

The exporter is attached to a session only when an OTLP endpoint is configured,
via either source:

- `observability.otel.endpoint` in `reyn.yaml` / `reyn.local.yaml`, or
- the standard `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable.

With no endpoint the exporter is never built — zero overhead, behavior
byte-identical to a build with no OTEL at all.

```yaml
observability:
  otel:
    endpoint: "http://localhost:4318"
    headers:
      Authorization: "Bearer ${OTEL_TOKEN}"
    service_name: "reyn"
    capture_content: false
```

Requires the OTEL SDK:

```
pip install reyn[observability]
```

An endpoint configured **without** the SDK installed logs a single warning and
stays not-attached (fail-open). See the
[`observability` config block](../config/reyn-yaml.md#observability-block) for
every field.

## Event → telemetry mapping

The mapping follows the OpenTelemetry **GenAI semantic conventions**. The
convention is pinned to a single version (see below) and every `gen_ai.*`
attribute key is a named constant, so the emitted attribute surface is auditable
in one place.

| P6 audit-event | OTLP output | Key attributes |
|----------------|-------------|----------------|
| `session_started` / `session_completed` | root span `session <agent>` | `gen_ai.agent.name`, `gen_ai.agent.id`, `gen_ai.conversation.id` |
| `turn_started` / `turn_completed` / `turn_cancelled` / `turn_settled` | turn span, child of the session | `gen_ai.operation.name` (`invoke_agent`); `turn_cancelled` sets an error status |
| `llm_called` + `llm_response_received` | child span `chat <model>` | `gen_ai.operation.name` (`chat`), `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `reyn.usage.cost_usd` |
| `llm_response_received` | metric histograms | `gen_ai.client.token.usage` (input/output), `gen_ai.client.cost.usd` |
| `tool_executed` / `mcp_called` / `mcp_failed` / `mcp_cancelled` | child span `execute_tool <name>` | `gen_ai.operation.name` (`execute_tool`), `gen_ai.tool.name` |
| `web_fetch_started` / `web_search_started` (+ completed/failed) | child span `execute_tool` | `gen_ai.tool.name`; the failed variant sets an error status |
| `permission_granted` / `permission_denied` / `user_intervention_*` / safety events | log record | `reyn.event.type`, `run_id`, `agent_id`, `actor`, `phase`, `intervention_id` |

Spans correlate into one trace per run: children nest under the open turn span,
turns under the session root, keyed by `run_id` (falling back to `agent_id`).
Events that arrive out of order or with a missing pair never crash the exporter —
a gap is skipped, and any span still open at process shutdown is closed and
flushed so no orphan span leaks.

### Pinned GenAI convention version

The GenAI semantic conventions are still Development-stability, so their
attribute names can change between releases. Reyn pins the convention version in
a single module constant (`GENAI_CONVENTION_VERSION = 1.37.0`). The exporter
emits only `gen_ai.*` keys defined by that pinned version; cost, which the GenAI
conventions do not cover, is emitted under the `reyn.*` namespace
(`reyn.usage.cost_usd`) rather than invented as a `gen_ai.*` key.

## Content is off by default (privacy)

P6 events are references and counts, not raw content. The exporter never
promotes a raw prompt or response body into a span or log record unless content
capture is explicitly enabled:

```yaml
observability:
  otel:
    capture_content: true   # opt in; use only against a trusted collector
```

With `capture_content: false` (the default) the telemetry carries token/cost
counts, model names, and event identifiers — never message bodies.

## Guarantees

- **Fail-open.** An OTLP endpoint that is unreachable, raising, or slow does not
  break the run. Every export path swallows its exceptions (latched to one
  warning), the inverse of the durability worker's fail-stop contract. The run
  completes normally and `.reyn/events` + the WAL are written exactly as without
  OTEL.
- **Off-loop.** Spans are batched and exported on a background thread; metrics
  export periodically. The event loop never blocks on the OTLP network path.
- **Recovery-independence.** OTEL is never a recovery source. With OTEL stopped,
  removed, or its endpoint lost, recovery and replay from `.reyn/events` + the
  WAL are byte-for-byte identical to a run with OTEL attached. OTEL absence does
  not change what is recovered.

## See also

- [Concepts: Events](../../concepts/runtime/events.md) — the P6 audit-event
  Source-of-Truth this surface subscribes to.
- [`.reyn/` directory layout](reyn-dir-layout.md) — recovery-core vs audit; OTEL
  adds no recovery-core state.
- [Config: `observability` block](../config/reyn-yaml.md#observability-block).
