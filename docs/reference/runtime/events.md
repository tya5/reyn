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

## Agent ID field (all events)

Every event emitted from a session whose `agent.id` is configured (in `reyn.yaml`) automatically carries an `agent_id` field in its payload. The default value is `reyn/<hostname>`. This enables RBAC and multi-agent audit trails per SOC2 / ISO 27001 / METI v1.1 requirements.

See [Concepts: multi-agent](../../concepts/multi-agent.md) — "Agent ID propagation" for details.

## Lifecycle events

| Kind | When | Key payload |
|------|------|-------------|
| `workflow_started` | First phase enters | `entry_phase`, `input_type`, `default_model` |
| `workflow_finished` | Skill completes cleanly | `phase`, `reason`, `confidence`, `total_phase_count`, `final_output_keys` |
| `phase_started` | Each phase visit begins | `phase`, `visit_count` |
| `phase_completed` | Each phase visit ends | `phase`, `next_phase`, `decision` |
| `phase_failed` | Phase raised an unrecoverable error | `phase`, `error` |
| `loop_limit_exceeded` | A phase exceeded `limits.phase.max_visits` | `phase`, `visit_count`, `max` |
| `phase_budget_exceeded` | A phase exceeded its wall-clock budget (`limits.phase.max_wall_seconds`) | `phase`, `elapsed`, `budget` |

## Plan lifecycle

Plan-mode events cover the full lifetime of a parallel plan — from decomposition through step execution to aggregation or interruption. See [Concepts: plan-mode](../../concepts/plan-mode.md) for the architectural context.

`plan_started` and `plan_completed` / `plan_aborted` are written to the agent WAL (`state_log.jsonl`), not the forensic event log — they appear in state recovery but not in the Events tab stream. The remaining events below are emitted to the regular events log and surface in the TUI Events tab under the `plan` filter.

| Kind | When | Key payload |
|------|------|-------------|
| `plan_started` | Plan execution begins (WAL only — not in events log) | `plan_id`, `goal`, `n_steps`, `target` (agent name) |
| `plan_step_started` | A step's sub-loop begins | `plan_id`, `step_id`, `depends_on` (list), `n_tools`, `chain_id` |
| `plan_step_completed` | A step's sub-loop finished successfully | `plan_id`, `step_id`, `content_len` (bytes of result text), `chain_id` |
| `plan_step_failed` | A step's sub-loop exhausted retries without success | `plan_id`, `step_id`, `error` (repr), `chain_id` |
| `plan_completed` | All steps finished; plan aggregation done (WAL only — not in events log) | `plan_id`, `target` |
| `plan_run_interrupted` | Plan loop exited via crash / cancel / unexpected exception (NOT `WorkflowAbortedError`) | `plan_id`, `exc_type` (exception class name), `chain_id` |
| `plan_aborted` | Plan was explicitly discarded via `/plan discard` or AgentRegistry cleanup after crash recovery (WAL only — not in events log) | `plan_id`, `reason`, `target` |

**Notes:**
- WAL-only events (`plan_started`, `plan_completed`, `plan_aborted`) appear in `state_log.jsonl` alongside the agent snapshot. They are not in `.reyn/events/*.jsonl` and do not show in the TUI Events tab.
- `plan_run_interrupted` is the crash/cancel signal; `plan_aborted` is the post-recovery cleanup signal. A crash followed by `reyn` restart will produce `plan_run_interrupted` (from the crashed session) then `plan_aborted` (from `AgentRegistry.restore_all`).
- `plan_step_started` is also written to WAL (via `record_plan_step_started`) in addition to the events log, to enable resume-time step pairing.

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
| `read_file`, `write_file`, `edit_file`, `delete_file`, `glob_files`, `grep`, `regenerate_index` | `file` op variants — all via `tool_executed` with `op=<sub_op>` |
| `shell_started`, `shell` (completed), `shell_timeout`, `shell_not_allowed` | `shell` op |
| `sandboxed_exec_started`, `sandboxed_exec_completed` | `sandboxed_exec` op — `started`: `argv`, `backend`; `completed`: `argv`, `backend`, `returncode` |
| `run_skill_started`, `skill_run_spawned`, `skill_run_failed` | `run_skill` op — `run_skill_started` carries `skill_version_hash: str` (sha256 hex of `skill.md` content at execution time; `"unknown"` if `skill.md` is absent) |
| `mcp_called`, `mcp_completed`, `mcp_failed` | MCP tool ops |
| `mcp_server_installed` | `mcp_install` op — `name`, key names only (no values) |
| `web_search_started`, `web_search_completed`, `web_search_failed`, `web_fetch_started` | search ops |
| `embed_progress` | `embed` op (Form B artifact reference only) — `embedded: int`, `skipped: int` cumulative per batch |
| `recall_embed_failed` | `recall` op — emitted when the embed sub-op fails; `query`, `error` |
| `index_dropped` | `index_drop` op — `source`, `chunks_dropped: int` |
| `skill_resolve_completed` | `skill_resolve` op — `name`, `resolved: bool`, `source: "local"\|"project"\|"stdlib"\|null` |
| `control_ir_skipped`, `control_ir_failed`, `control_ir_validation_error` | dispatch failures (`control_ir_skipped` reasons include `shell_not_allowed`, `handler_not_implemented`, `not_allowed_in_phase`) |
| `permission_denied` | When an op is denied by the resolver |

## Credentials and OAuth

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `sub_skill_credential_scope` | Emitted by the `run_skill` op handler at sub-skill entry, after the OS computes the effective credential scope (intersection of the sub-skill's `required_credentials` with the parent scope). | `skill: str` — sub-skill reference (same value as `op.skill`); `allowed_keys: list[str]` — sorted, deduplicated list of allowed secret keys, or `["*"]` if the effective scope is unrestricted. |
| `token_refreshed` | Emitted by `reyn.secrets.get_valid_token(key)` after a successful OAuth refresh against the provider's token endpoint (RFC 6749 §6). | `key: str` — OAuth token key (same as the `~/.reyn/oauth_tokens.json` entry); `expires_at: str` — ISO-8601 timestamp of the new access token's expiry. |
| `token_refresh_failed` | Emitted by `get_valid_token` when the token endpoint returns a non-2xx response or the response payload is malformed. Raises `OAuthRefreshError`. | `key: str`; `error: str` — short error description (HTTP status + provider error code if available). |

**Notes:**
- `sub_skill_credential_scope` is audit-grade; used to reconstruct the credential authorisation chain across nested skill runs. Pairs with `run_skill_started` (same `skill` name).
- `token_refresh_failed` pairs with `token_refreshed` — exactly one is emitted per `get_valid_token` call that performs a network refresh.

See also: [Concepts: secret handling](../../concepts/secret-handling.md) — OAuth lifecycle and credential scoping; [Concepts: permission model](../../concepts/permission-model.md) — per-skill credential scoping; [DSL reference: `required_credentials`](../dsl/skill-md.md).

## Action catalog routing

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `routing_decided` | Emitted by the universal action catalog dispatch path when an action wrapper (`list_actions` / `search_actions` / `describe_action` / `invoke_action`) routes a request. | `action_name: str`; `source: str` — `"catalog"` \| `"hot_alias"` \| `"direct"`; `outcome: str` — `"dispatched"` \| `"deflected"` \| `"error"`; `chain_id: str` — request chain identifier for cross-call correlation. |

**Notes:** enables auditing the wrapper-only routing path. Cross-correlate with `chain_id` across the action's downstream events.

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
| `agent_message_refused` | A send was refused (e.g. exceeded `safety.loop.max_agent_hops`) | `reason`, `to_agent`, `depth`, `chain_id` |
| `chain_timeout` | A pending chain exceeded `safety.timeout.chain_seconds` and was force-resolved with a synthetic error response upstream | `chain_id`, `waiting_on` (sorted list of agents that hadn't replied), `timeout_seconds`, `origin_agent` |

`chain_id` is uuid4 hex; one per top-level user submission, propagated unchanged across every hop. Cross-agent reconstruction is `grep <chain_id>` over each agent's `events.jsonl` plus `history.jsonl`.

## Workspace

| Kind | When |
|------|------|
| `workspace_updated` | Any artifact is written |
| `tool` / `tool_executed` | Generic tool dispatch |

## Skill management {#skill-management}

| Kind | Payload fields | Emitted when |
|------|---------------|--------------|
| `skill_rolled_back` | `skill: str`, `from_version: int`, `to_version: int`, `reason: str` (default `"user rollback via CLI"`) | A `reyn skill rollback` invocation restores a prior version. Written to `.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl`. See [Reference: CLI — `reyn skill rollback`](../cli/skill.md). |

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
