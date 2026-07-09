---
type: concept
topic: architecture
audience: [human, agent]
---

# Events

Every state change in reyn emits an audit-event. The audit-event log is the runtime's diary: a JSONL stream that records what happened, in order, with enough detail to replay the run.

## Why everything is an audit-event

There is no separate logger, tracer, or telemetry hook. The same channel powers:

- **Live debug output.** Console reporters subscribe to the audit-event stream and render each audit-event as it arrives.
- **Replay.** `reyn events <log_file>` re-renders a saved log to the console without re-invoking the LLM.
- **Eval analytics.** Eval reports aggregate audit-event data (token usage, validation errors) per case.
- **Crash recovery (implemented).** Crash recovery reconstructs agent state from the WAL (`.reyn/state/wal.jsonl`) plus seq-keyed snapshots as its substrate — not the audit-event log. User-facing rewind/resume (PITR + global rewind) is a separate design; see [Time-travel](time-travel.md).

If the OS is the only mutator (P3) and every mutation emits an audit-event, the audit-event log is sufficient. There's no "what else happened" to chase down.

## What gets recorded

A few of the larger buckets:

- **LLM and context** — `llm_called`.
- **Control IR** — one audit-event per op kind (`read_file`, `sandboxed_exec_started`, `mcp_called`, `web_search_started`, `recall_embed_failed`, etc.) plus `permission_denied`.
- **User interaction** — `user_message_received`, `user_intervention_received`, `chat_started`, `chat_stopped`, `turn_cancelled`.
- **Agent-to-agent messaging** — `agent_message_sent`, `agent_request_received`, `agent_response_received`, `agent_message_refused`, `chain_timeout`. Each carries `chain_id` so a single user request can be traced across hops.
- **Task management** — `task_op`, `task_readiness`, `task_disposition`, `task_dependency_aborted`.

The full taxonomy lives in the [events reference](../../reference/runtime/events.md).

### Task subscription events — WAL, not audit-event log

Task↔session binding changes (`task_subscribed`, `task_rebound`) are recorded in the **WAL** (StateLog, `.reyn/state/wal.jsonl`) — not in the P6 audit-event log. The WAL is the crash-recovery and time-travel substrate; the audit-event log is the per-run trace. They are separate logs with different durability contracts (see [Time-travel](time-travel.md) — *WAL vs audit-event separation*). Do not look for `task_subscribed` in the audit-event log; it is not there.

## What an audit-event is

Every audit-event has a stable envelope:

```
type      — event type (see reference)
timestamp — ISO-8601 timestamp
data      — flat dict of payload fields specific to the type
```

Key fields present in most audit-events (in `data`):

```
run_id    — uuid for the run (present on most run-scoped audit-events)
```

Note: `run_id` is present on most run-scoped audit-events (`llm_called`,
`permission_denied`, etc.) but absent from some audit-events emitted outside
a run context (e.g. `chat_started`).

### Audit-events with required fields (FP-0021)

A growing set of audit-event kinds are required to carry specific
audit fields in their `data` dict (the required fields vary per kind — e.g.
`llm_called` requires `model`; `permission_granted`/`permission_denied`
require `run_id`, `actor`, `phase`). The authoritative, current registry
lives in `src/reyn/core/events/event_schema.py`
(`EVENT_AUDIT_REQUIREMENTS`) — this list has grown over time and will keep
growing, so it is not duplicated here. Dedicated per-feature invariant tests
(e.g. `tests/test_session_lifecycle_events_1800.py`,
`tests/test_mcp_search_tool_invariants.py`,
`tests/test_chat_turn_completed_inline.py`) each assert that their event
kinds are declared here with the correct required fields, on every CI run.

Enforcement is test-time only (not at `emit()` runtime) to keep
production overhead zero.

Stable shape makes the log machine-readable without a custom parser per consumer.

## What audit-events are NOT

- **Not application logs.** A workflow author shouldn't emit free-form audit-events. The set is OS-defined.
- **Not memory.** Audit-events are the runtime's per-run record; memory is across-run knowledge. See [../data-retrieval/memory.md](../data-retrieval/memory.md).
- **Not the source of truth for artifacts.** Artifacts pass through the workspace channel; audit-events record that they passed.

## Reading audit-events as a debugging tool

When something looks wrong:

1. Find the run id from the run output's last line (`events saved → ...`).
2. `reyn events .reyn/events/<run_id>.jsonl --conversation` to see what each LLM call looked like and what it returned.
3. Or `--filter permission_denied` to jump straight to where the OS refused an op.

You don't need a debugger; the log already has the information.

## See also

- [Reference: events](../../reference/runtime/events.md) — the full audit-event taxonomy

Audit-events are the per-run trace, not the crash-recovery or time-travel
substrate — those are WAL-backed (see [Time-travel](time-travel.md)). For
payload-level trace inspection, see
[reference/dogfood-tracing.md](../../reference/dogfood-tracing.md).
