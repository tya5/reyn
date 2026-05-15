---
type: reference
topic: runtime
audience: [human, agent]
---

# Events

reyn emits a structured event for every state change. The full event log is JSONL, written to `.reyn/events/<run_id>.jsonl` and replayable with `reyn events <log_file>`.

## Event envelope

Every event has:

```json
{
  "ts": "2026-04-30T10:00:00.123Z",
  "kind": "<event_kind>",
  "phase": "<current_phase>",
  "run_id": "<uuid>",
  ... // kind-specific payload
}
```

## Lifecycle events

| Kind | When | Key payload |
|------|------|-------------|
| `workflow_started` | First phase enters | `entry_phase`, `input_artifact_type` |
| `workflow_finished` | Skill completes cleanly | `phase`, `reason`, `confidence`, `total_phase_count`, `final_output_keys` |
| `phase_started` | Each phase visit begins | `phase`, `visit_count` |
| `phase_completed` | Each phase visit ends | `phase`, `next_phase`, `decision` |
| `phase_failed` | Phase raised an unrecoverable error | `phase`, `error` |
| `loop_limit_exceeded` | A phase exceeded `limits.phase.max_visits` | `phase`, `visit_count`, `max` |
| `phase_budget_exceeded` | A phase exceeded its wall-clock budget (`limits.phase.max_wall_seconds`) | `phase`, `elapsed`, `budget` |

## LLM and context

| Kind | Key payload |
|------|-------------|
| `context_built` | `phase`, `candidate_count`, `prompt_token_estimate` |
| `llm_called` | `phase`, `model`, `input_tokens`, `output_tokens`, `latency_ms` |
| `validation_error` | What the LLM emitted that the OS rejected |
| `normalization_error` | LLM output couldn't be parsed at all |

## Control IR

Each Control IR op kind emits its own event:

| Kind | When |
|------|------|
| `read_file`, `write_file`, `edit_file`, `delete_file`, `glob_files`, `grep` | `file` op variants |
| `shell_started`, `shell` (completed), `shell_timeout`, `shell_not_allowed` | `shell` op |
| `run_skill_started`, `skill_run_spawned`, `skill_run_failed` | `run_skill` op — `run_skill_started` carries `skill_version_hash: str` (sha256 hex of `skill.md` content at execution time; `"unknown"` if `skill.md` is absent) |
| `mcp_called`, `mcp_completed`, `mcp_failed` | MCP tool ops |
| `web_search_started`, `web_search_completed`, `web_search_failed`, `web_fetch_started` | search ops |
| `control_ir_skipped`, `control_ir_failed`, `control_ir_validation_error` | dispatch failures (`control_ir_skipped` reasons include `shell_not_allowed`, `handler_not_implemented`, `not_allowed_in_phase`) |
| `permission_denied` | When an op is denied by the resolver |

## User interaction

| Kind | When |
|------|------|
| `user_message_received` | A new user turn enters the runtime. Carries `chain_id` (the uuid minted by `submit_user_text` and propagated through any agent-to-agent messages this turn produces) |
| `user_intervention_received` | An `ask_user` op got its answer |
| `chat_started`, `chat_stopped` | Chat session lifecycle |

## Skill spawning (chat)

| Kind | When |
|------|------|
| `skill_run_spawned` | A skill was launched from a router decision (`run_id`, `skill`) |
| `skill_spawn_refused` | `_spawn_skill` rejected a skill not in the agent's `allowed_skills`. Payload: `reason="allowlist"`, `skill`, `agent` |

## Agent-to-agent messaging

| Kind | When | Key payload |
|------|------|-------------|
| `agent_message_sent` | `_send_to_agent` or `_send_agent_response` delivered a payload | `kind=agent_request\|agent_response`, `from_agent`, `to_agent`, `depth`, `chain_id` |
| `agent_request_received` | Receiving agent pulled an `agent_request` from its inbox | `from_agent`, `depth`, `chain_id` |
| `agent_response_received` | Originating agent pulled an `agent_response` from its inbox | `from_agent`, `depth`, `chain_id` |
| `agent_message_refused` | A send was refused (e.g. exceeded `multi_agent.max_hop_depth`) | `reason`, `to_agent`, `depth`, `chain_id` |
| `chain_timeout` | A pending chain exceeded `multi_agent.chain_timeout_seconds` and was force-resolved with a synthetic error response upstream | `chain_id`, `waiting_on` (sorted list of agents that hadn't replied), `timeout_seconds`, `origin_agent` |

`chain_id` is uuid4 hex; one per top-level user submission, propagated unchanged across every hop. Cross-agent reconstruction is `grep <chain_id>` over each agent's `events.jsonl` plus `history.jsonl`.

## Workspace

| Kind | When |
|------|------|
| `workspace_updated` | Any artifact is written |
| `tool` / `tool_executed` | Generic tool dispatch |

## Skill management {#skill-management}

| Kind | Payload fields | Emitted when |
|------|---------------|--------------|
| `skill_rolled_back` | `skill: str`, `from_version: int`, `to_version: int`, `reason: str` (default `"user rollback via CLI"`) | A `reyn skill rollback` invocation restores a prior version. **Currently NOT emitted** — no active EventStore in the standalone CLI context. Planned to be wired in a follow-up PR. See [Reference: CLI — `reyn skill rollback`](../cli/skill.md). |

## Replay

```bash
reyn events .reyn/events/<run_id>.jsonl
```

Replays the log to the console with the same formatting as a live run. The LLM is not re-invoked — replay is purely for inspection.

## Why everything is an event

Two consequences fall out of "every state change emits":

- **Replayability.** A saved log is a complete record of execution. Future checkpoint/resume designs (see roadmap) build on this.
- **Observability with no bolt-on.** No separate logger, tracer, or telemetry hook — the same channel powers debug output, replay, and (eventually) eval analytics.

## See also

- [run.md](../cli/run.md) — `--events` flag
- [control-ir.md](control-ir.md) — Control IR ops
- [Concepts: events](../../concepts/events.md)
