---
type: concept
topic: architecture
audience: [human, agent]
---

# Events

Every state change in reyn emits an event. The event log is the runtime's diary: a JSONL stream that records what happened, in order, with enough detail to replay the run.

## Why everything is an event

There is no separate logger, tracer, or telemetry hook. The same channel powers:

- **Live debug output.** Console reporters subscribe to the event stream and render each event as it arrives.
- **Replay.** `reyn events <log_file>` re-renders a saved log to the console without re-invoking the LLM.
- **Eval analytics.** Eval reports aggregate event data (token usage, phase counts, validation errors) per case.
- **Future checkpoint/resume.** A complete event log is, by construction, a complete description of execution. Resume from N is a matter of replaying the log up to event N.

If the OS is the only mutator (P3) and every mutation emits an event, the log is sufficient. There's no "what else happened" to chase down.

## What gets recorded

Three big buckets, plus a few smaller ones:

- **Lifecycle** — `workflow_started`, `phase_started`, `phase_completed`, `workflow_finished`, `phase_failed`, `loop_limit_exceeded`.
- **LLM and context** — `context_built`, `llm_called`, `validation_error`, `normalization_error`.
- **Control IR** — one event per op kind (`read_file`, `write_file`, `shell_started`, `run_skill_started`, etc.) plus `permission_denied`.
- **Chat lifecycle** — `chat_started`, `chat_stopped`, `user_message_received`, `skill_run_spawned`, `skill_spawn_refused`.
- **Agent-to-agent messaging** — `agent_message_sent`, `agent_request_received`, `agent_response_received`, `agent_message_refused`. Each carries `chain_id` so a single user request can be traced across hops.

The full taxonomy lives in the [events reference](../../reference/runtime/events.md).

## What an event is

Every event has a stable envelope:

```
type      — event type (see reference)
timestamp — ISO-8601 timestamp
data      — flat dict of payload fields specific to the type
```

Key fields present in most events (in `data`):

```
run_id    — uuid for the run (always present for skill-execution events)
skill     — skill name (always present for skill-execution events)
phase     — current phase at emission time
```

Note: `run_id` and `skill` are present on lifecycle events (`workflow_started`,
`workflow_finished`, `llm_called`, etc.) but absent from some events emitted outside
a run context (e.g. `chat_started`).

### Events with audit fields (FP-0021)

Eight event kinds are now required to carry specific
audit fields in their `data` dict.  The authoritative registry lives in
`src/reyn/events/event_schema.py` (`EVENT_AUDIT_REQUIREMENTS`).  A Tier 2
invariant test (`tests/test_event_audit_invariants.py`) verifies that each
event kind carries its declared fields on every CI run.

| Event kind                      | Required fields                         |
|---------------------------------|-----------------------------------------|
| `workflow_started`              | `run_id`, `skill`                       |
| `workflow_finished`             | `run_id`, `skill`                       |
| `llm_called`                    | `run_id`, `skill`                       |
| `llm_response_received`         | `run_id`, `skill`                       |
| `permission_granted`            | `run_id`, `skill`, `phase`              |
| `permission_denied`             | `run_id`, `skill`, `phase`              |
| `user_intervention_requested`   | `run_id`, `skill`, `intervention_id`   |
| `user_intervention_received`    | `run_id`, `skill`, `intervention_id`   |

Enforcement is test-time only (not at `emit()` runtime) to keep
production overhead zero.

Stable shape makes the log machine-readable without a custom parser per consumer.

## What events are NOT

- **Not application logs.** A skill author shouldn't emit free-form events. The set is OS-defined.
- **Not memory.** Events are the runtime's per-run record; memory is across-run knowledge. See [../data-retrieval/memory.md](../data-retrieval/memory.md).
- **Not the source of truth for artifacts.** Artifacts pass through the workspace channel; events record that they passed.

## Reading events as a debugging tool

When something looks wrong:

1. Find the run id from the run output's last line (`events saved → ...`).
2. `reyn events .reyn/events/<run_id>.jsonl --conversation` to see what each LLM call looked like and what it returned.
3. Or `--filter validation_error --filter normalization_error` to jump straight to where the OS rejected output.

You don't need a debugger; the log already has the information.

## See also

- [Reference: events](../../reference/runtime/events.md) — the full event taxonomy
- [Reference: events CLI](../../reference/cli/run.md) — `--events` flag on `reyn run`
- [How-to: debug with events](../../guide/for-skill-authors/operations/debug-with-events.md)

Events are not just an audit trail — they are the substrate for time-travel
debugging. See [reference/dogfood-tracing.md](../../reference/dogfood-tracing.md)
for `--mode replay` (step-by-step walk of a past run) and `--mode compare`
(side-by-side diff between two runs).
