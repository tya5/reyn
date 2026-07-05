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
  "type": "<event_kind>",
  "timestamp": "2026-04-30T10:00:00.123456+00:00",
  "data": {
    ... // kind-specific payload; may include agent_id / run_id when the
        // emitting EventLog was configured with them (see below)
  }
}
```

## Agent ID field (all events)

Every event emitted from a session whose `agent.id` is configured (in `reyn.yaml`) automatically carries an `agent_id` field in its payload. The default value is `reyn/<hostname>`. This enables RBAC and multi-agent audit trails per SOC2 / ISO 27001 / METI v1.1 requirements.

See [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) — "Agent ID propagation" for details.

## LLM and context

| Kind | Key payload |
|------|-------------|
| `llm_called` | `phase`, `model`, `input_tokens`, `output_tokens`, `latency_ms` |

## Control IR

Each Control IR op kind emits its own event:

| Kind | When |
|------|------|
| `read_file`, `write_file`, `edit_file`, `delete_file`, `glob_files`, `grep`, `regenerate_index` | `file` op variants — all via `tool_executed` with `op=<sub_op>` |
| `sandboxed_exec_started`, `sandboxed_exec_completed` | `sandboxed_exec` op — `started`: `argv`, `backend`; `completed`: `argv`, `backend`, `returncode` |
| `mcp_called`, `mcp_completed`, `mcp_failed` | MCP tool ops |
| `mcp_server_installed` | `mcp_install` op — `name`, key names only (no values) |
| `web_search_started`, `web_search_completed`, `web_search_failed` | web_search ops — `started`: `query`, `backend`; `completed`: adds `result_count`; `failed`: adds `error` |
| `web_fetch_started`, `web_fetch_completed`, `web_fetch_failed` | web_fetch ops — `started`: `url`; `completed`: `url`, `status_code`, `content_length`, `extractor`; `failed`: `url`, `status` (`"timeout"` or `"error"`), `error` |
| `recall_embed_failed` | `recall` op — emitted when the embed sub-op fails; `query`, `error` |
| `index_dropped` | `index_drop` op — `source`, `chunks_dropped: int` |
| `control_ir_skipped`, `control_ir_failed` | dispatch failures (`control_ir_skipped` reasons include `handler_not_implemented`, `not_allowed_in_phase`) |
| `permission_denied` | When an op is denied by the resolver |

## MCP

Unlike the Control IR `mcp_*` events above (tied to a tool-call op), these
fire asynchronously from the MCP connection/receive-loop, independent of any
op dispatch:

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `mcp_initialized` | Emitted on every (re)connect, once the server's `initialize` handshake completes. | `server`, `negotiated_version`, `capabilities` |
| `mcp_resource_updated` | A subscribed resource's server-pushed `resources/updated` notification, or a synthetic resync fired per re-subscribed URI after a transport-death reconnect. Also wired into the hook dispatcher as an external-event hook-point — see [Concepts: hooks](../../concepts/runtime/hooks.md#mcp_resource_updated). | `server`, `uri`, `resync` (`true` for a reconnect resync, `false` for a real push) |
| `mcp_elicitation_requested` | A server issues an `elicitation/create` structured-input request. | `server`, `field_keys` (the requested schema's property **names** only — never values) |
| `mcp_elicitation_answered` | The request resolves to `accept` or `decline` (human choice, or a `decline` from `auto_decline` config). | `server`, `field_keys`, `action` (`"accept"` \| `"decline"`) |
| `mcp_elicitation_timed_out` | No answer arrived before `elicitation_timeout_seconds`. | `server`, `field_keys` |
| `mcp_elicitation_auto_declined` | Declined without prompting — `reason` distinguishes a server configured `elicitation: auto_decline` from a headless context (no live intervention listener). | `server`, `field_keys`, `reason` (`"server_configured"` \| `"headless"`) |

None of these events include the human's typed answer or any field *value* —
only the requested schema's property names, matching the sensitive-field
handling described in [Concepts: MCP § Elicitation](../../concepts/tools-integrations/mcp.md#elicitation-structured-input-requests-from-a-server).

## Credentials and OAuth

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `token_refreshed` | Emitted by `reyn.secrets.get_valid_token(key)` after a successful OAuth refresh against the provider's token endpoint (RFC 6749 §6). | `key: str` — OAuth token key (same as the `~/.reyn/oauth_tokens.json` entry); `expires_at: str` — ISO-8601 timestamp of the new access token's expiry. |
| `token_refresh_failed` | Emitted by `get_valid_token` when the token endpoint returns a non-2xx response or the response payload is malformed. Raises `OAuthRefreshError`. | `key: str`; `error: str` — short error description (HTTP status + provider error code if available). |

**Notes:**
- `token_refresh_failed` pairs with `token_refreshed` — exactly one is emitted per `get_valid_token` call that performs a network refresh.

See also: [Concepts: secret handling](../../concepts/runtime/secret-handling.md) — OAuth lifecycle and credential scoping; [Concepts: permission model](../../concepts/runtime/permission-model.md) — per-skill credential scoping.

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
| `turn_cancelled` | A user turn was cancelled mid-router-loop (e.g. `/cancel` or a new submission supersedes the running turn). Payload: `chain_id`. |

## Task management

Events emitted by the task Control IR ops (`task.py`).

| Kind | When | Key payload |
|------|------|-------------|
| `task_op` | Any mutating task operation completes (create / update-status / complete / abort) | `op` (op kind string), `task_id`, plus op-specific fields |
| `task_readiness` | A task transitions to `ready` or `blocked` (OS re-derive changed readiness) | `task_id`, `to` (`"ready"` or `"blocked"`), `trigger` (task_id of the op that caused the change) |
| `task_disposition` | Each task in an aborted subtree reaches its terminal disposition | `task_id`, `disposition` (`"aborted"`), `requester`, `origin`, `root` (task_id of the root abort op) |
| `task_dependency_aborted` | A task's dependency reached a non-completed terminal; its requester is notified to decide recovery (§16) | `task_id` (the terminal task), `disposition`, `requester` (session or task id — the §16 notify-target), `dependents` (list of task_ids that are now stuck) |

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
| `tool_executed` | Generic tool dispatch |

## Memory

| Kind | When | Key payload |
|------|------|-------------|
| `memory_saved` | The `memory` tool persisted a memory file to a layer | `layer`, `slug`, `path` |
| `memory_deleted` | The `memory` tool deleted a memory file | `layer`, `slug`, `path` |

## Compaction and context budget

These fire on chat turns as the context-budget advisor and compaction
controller evaluate whether history needs summarising. Most carry a
"checked but did not compact" outcome — they are high-frequency and
mostly informational.

| Kind | When | Key payload |
|------|------|-------------|
| `compaction_check` | The compaction gate ran for a turn. `outcome` records the decision — e.g. `too_few_turns`, `below_min_batch`, `pre_frame_overflow`, `already_running`, `forced_sync`, `forced_sync_no_turns`. Some outcomes also carry `turns`, `head`, `tail`. | `outcome`, plus outcome-specific fields |
| `compaction_failed` | A compaction attempt raised. | `error` |
| `compact_op_unavailable` | The `compact` Control IR op was dispatched in a context where no compaction engine is wired. | `run_id`, `phase` |
| `summary_resummarize_failed` | Re-summarising an existing summary (nested compaction) raised. | `error` |
| `budget_reset` | The chat budget gateway reset its per-window accounting. | `before` (prior accumulated value) |

## Safety limits

See [Concepts: safety framework](../../concepts/runtime/safety.md) for the
intervention flow and force-close wrap-up.

| Kind | When | Key payload |
|------|------|-------------|
| `limit_denied` | A safety limit was denied (no extension granted) and the OS is about to attempt the force-close wrap-up. | `kind` (`max_iterations` \| `router_cap`), `chain_id`, plus `limit` (router iterations) or `count`/`cap` (router cap) |

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

- [control-ir.md](control-ir.md) — Control IR ops
- [Concepts: events](../../concepts/runtime/events.md)
