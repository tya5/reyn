---
type: reference
topic: runtime
audience: [human, agent]
---

# Events

reyn emits a structured event for every state change. The full event log is JSONL, written to `.reyn/events/<run_id>.jsonl` and replayable with `reyn events <log_file>`.

## Kind vocabulary

The `type` field is drawn from a **closed set**. Every kind reyn emits is listed
below, and reyn emits nothing else — so a subscriber can enumerate the complete
set of types it may receive, and a handler written for a kind not on this list
will never fire.

The source of truth is `AUDIT_EVENT_KINDS` in
`src/reyn/core/events/event_schema.py`; this list is checked against it in CI,
and that declaration is in turn checked against the emitting code — in both
directions, so neither a kind without a declaration nor a declaration without a
producer can land. Adding a kind is a public-interface change, and it shows up
as a diff on this page.

Two things this list is deliberately *not*:

- **Not the `op` values.** `read_file`, `write_file`, `grep` and their siblings
  are values of the `op` field inside the shared `tool_executed` kind, not kinds
  of their own. They are a different axis and are not part of this vocabulary.
- **Not a field contract.** Which payload fields a given kind must carry is a
  separate registry (`EVENT_AUDIT_REQUIREMENTS`, same module) covering a subset
  of these kinds; the per-kind sections below document the payloads in prose.

<!-- BEGIN audit-event-kinds -->

```text
agent_delta
agent_message_refused
agent_message_sent
agent_request_received
agent_response_received
asyncio_unhandled_exception
body_summary_hard_truncated
budget_reset
bus_subscriber_dropped
canonical_degraded
canonical_fallback_used
chain_peer_discarded
chain_timeout
chain_timeout_extended
chat_started
chat_stopped
chat_turn_completed_inline
client_attached
client_detached
client_seized
compact_op_completed
compact_op_failed
compact_op_requested
compact_op_unavailable
compaction_batch_cap_below_head_tail_budget
compaction_check
compaction_completed
compaction_failed
compaction_shrink_recovered
compaction_started
composer_dropped
composer_fired
config_reload_rejected
config_reloaded
control_ir_failed
control_ir_skipped
direct_alias_call_salvaged
elide_evaluated
embed_attempts
embed_cancelled
embed_secret_redacted
embedding_index_build_complete
embedding_index_build_error
embedding_index_build_progress
embedding_index_build_started
exec_threat_blocked
exec_threat_match
file_read_media_denied
force_close_triggered
hook_event_emitted
hook_push_fired
hook_shell_executed
inbox_cancel
index_dropped
index_update_cost_warning
index_updated
intervention_answer_submitted
intervention_denied
intervention_routed
limit_denied
llm_call_retry
llm_call_retry_exhausted
llm_called
llm_request
llm_request_error
llm_response_received
mcp_called
mcp_cancelled
mcp_completed
mcp_elicitation_answered
mcp_elicitation_auto_declined
mcp_elicitation_requested
mcp_elicitation_timed_out
mcp_failed
mcp_initialized
mcp_install_cancelled
mcp_install_probe_failed
mcp_install_threat_blocked
mcp_install_threat_match
mcp_progress
mcp_prompt_get
mcp_prompt_get_cancelled
mcp_prompt_get_completed
mcp_prompt_get_failed
mcp_prompt_list_changed
mcp_prompts_listed
mcp_resource_read
mcp_resource_read_cancelled
mcp_resource_read_completed
mcp_resource_read_failed
mcp_resource_subscribe
mcp_resource_subscribe_cancelled
mcp_resource_subscribe_failed
mcp_resource_subscribed
mcp_resource_templates_listed
mcp_resource_unsubscribe
mcp_resource_unsubscribe_cancelled
mcp_resource_unsubscribe_failed
mcp_resource_unsubscribed
mcp_resource_updated
mcp_resources_listed
mcp_server_install_skipped
mcp_server_installed
mcp_server_removed
mcp_tool_list_changed
mcp_tool_probe_degraded
mcp_tools_listed
memory_deleted
memory_saved
model_budget_fallback
model_cost_block
model_cost_warn
network_ssl_verify_disabled
new_msg_exceeds_budget
oauth_login_completed
oauth_login_started
peer_reply_failed_surfaced
pending_intervention_claimed
pending_intervention_discarded
permission_denied
permission_granted
pipeline_install_threat_blocked
pipeline_install_threat_match
pipeline_installed
pipeline_load_failed
pipeline_run_attached
pipeline_step_completed
pipeline_step_started
plan_step_llm_memoized
plugin_install_completed
plugin_install_copied
plugin_install_reconciled
plugin_install_registered
plugin_install_started
plugin_uninstall_completed
plugin_uninstall_registry_dropped
plugin_uninstall_started
presentation_install_blocked
presentation_installed
presentation_load_failed
presented
project_context_changed
repo_ingest_files_skipped
resource_cap_exceeds_budget_trigger
router_context_overflow_detected
router_context_overflow_unrecovered
router_empty_response_detected
router_empty_response_retry_injected
router_loop_terminated_by_exception
router_represent_round
router_retry_exhausted
routing_decided
safety_limit_checkpoint
sandbox_axis_unenforced
sandbox_policy_narrowed
sandbox_policy_not_applied
sandboxed_exec_cancelled
sandboxed_exec_completed
sandboxed_exec_started
secret_cleared
secret_rotated
secret_set
semantic_search_complete
semantic_search_embed_failed
semantic_search_started
session_completed
session_halted
session_restored
session_started
skill_body_loaded
skill_install_threat_blocked
skill_install_threat_match
skill_installed
skill_invoke_body_loaded
skill_invoke_collision
state_change_notified
summary_resummarize_failed
summary_resummarized
task_settle_undelivered
threat_block
threat_scan_match
token_refresh_failed
token_refreshed
tool_call_cap_exceeded
tool_call_deduped
tool_called
tool_cycle_kept_whole_over_budget
tool_executed
tool_failed
tool_result_offloaded
tool_returned
turn_cancelled
turn_completed
turn_settled
turn_started
turn_too_large_truncated
untrusted_narrowing_engaged
user_answered_intervention
user_intervention_received
user_intervention_requested
user_message_received
user_submitted
web_fetch_completed
web_fetch_failed
web_fetch_media_denied
web_fetch_ssrf_blocked
web_fetch_started
web_fetch_too_large
web_fetch_too_many_redirects
web_search_completed
web_search_failed
web_search_started
workspace_updated
```

<!-- END audit-event-kinds -->

## Event envelope

Every event has:

```json
{
  "type": "<event_kind>",
  "timestamp": "2026-04-30T10:00:00.123456+00:00",
  "data": {
    ... // kind-specific payload; may include agent_id / run_id when the
        // emitting EventLog was configured with them (see below), plus
        // audit_seq / emitter on every event from a session-path EventLog
        // (see "Audit sequence" below)
  }
}
```

## Agent ID field (all events)

Every event emitted from a session whose `agent_id` is configured (in `reyn.yaml`) automatically carries an `agent_id` field in its payload. The default value is `reyn/<hostname>`. This enables RBAC and multi-agent audit trails per SOC2 / ISO 27001 / METI v1.1 requirements.

See [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) — "Agent ID propagation" for details.

## Audit sequence (`audit_seq` / `emitter`, all events)

Every event carries `emitter` (a string identifying **one execution** of an
`EventLog` — not the session's stable identity, a fresh token minted at
construction time, one per real process run) and, when that EventLog tracks
sequencing (the default), a monotonically increasing `audit_seq` starting
at 1.

**`audit_seq` is NOT the WAL's `seq`.** The WAL's `seq` is a recovery
coordinate (where replay resumes reconstructing in-memory state);
`audit_seq` is purely an audit-continuity witness — a subscriber uses it to
detect a DROPPED event (`(emitter, audit_seq)` pairs with a gap in the
number) without needing in-order delivery. The two are deliberately
different names for two different concepts (this repo has hit the "same
name, two meanings" defect class more than once — see CLAUDE.md).

**Monotonic per `emitter`, never across emitters.** Reconstructing global
ordering across two different emitters (two sessions, or two runs of the
same session) is not this field's job — that would require cross-process
coordination at the point of emission, which the audit band deliberately
does not impose (a synchronization point on the execution path, for
audit's sake, is the opposite of what the band exists to be). A reader
groups events by `emitter` first, then checks each group's own
`audit_seq` run for gaps.

**A new process execution is a new `emitter`, never a resumed count.** If a
session's `EventLog` reused a stable identity (e.g. the session_id alone)
across a restart, a fresh process would re-emit `audit_seq` 1..N under the
SAME emitter a prior process already used — a reader could no longer tell
"event 5 is missing" from "this is a new run's own event 5". A fresh
`EventLog` instance is constructed exactly once per real process execution
and never reloaded across a restart, so minting `emitter` once at
construction (rather than deriving it from any longer-lived identity)
gives per-execution uniqueness by construction, with no counter to persist
or reconcile across a restart.

**Neither field is caller-settable per emit call** (unlike `agent_id`/
`run_id` above, which follow a caller-wins convention) — `EventLog.emit`
always overwrites both, even if a caller's own `data` happens to include
same-named keys. Numbering the events is `emit`'s own job, done in exactly
one place; letting a caller influence it would open a path to skip or
forge a number, which would defeat the gap-detection contract 3 exists
for.

**`.reyn/events/direct/cli/` is out of gap-detection scope.** A one-off CLI
event (`emit_cli_event`, no active session) carries `emitter: "cli"` but no
`audit_seq` at all — a single event from a single process invocation is
not a series a gap can be detected in, so a meaningless `audit_seq: 1`
every time is omitted rather than emitted.

## LLM and context

| Kind | Key payload |
|------|-------------|
| `llm_called` | `model` (+ `chain_id` when the call belongs to a delegation chain) |
| `llm_response_received` | `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_creation_tokens`, `cost_usd`, `usage_source` (+ `chain_id`) |
| `embedding_index_build_complete` | `source_id`, `chunk_count`, `total_tokens`, `cost_usd`, `embedding_model` (a disk-adopt/no-fresh-build completion carries `total_tokens`/`cost_usd` as `null` — no embed call happened that run, not a cost of zero) |
| `repo_ingest_files_skipped` | #4431 — the repo-knowledge (`knowledge_repo_doc`/`knowledge_repo_src`) background build excluded one or more files for exceeding `_REPO_INGEST_MAX_BYTES` (256 KB, `src/reyn/data/index/knowledge_ingest.py`). Emitted once per build, only when the count is nonzero — a routine build with nothing skipped emits no event at all. `kind` (`"doc"`/`"src"`), `skipped_count`, `reason` (currently always `"over_size_cap"`) |
| `resource_cap_exceeds_budget_trigger` | #4381 PR-1 — `resolve_effective_trigger_and_budgets`'s own invariant check (`src/reyn/runtime/services/router_history_buffer.py`): the per-result resource boundary (`control_ir_inline_cap`, bytes, converted to tokens via `context_builder.INLINE_CAP_BYTES_PER_TOKEN`) exceeds the budget boundary (`effective_trigger`, the model's context window). Detection only — no value is clamped. Warn-once per `(model, phase)`, not per call (this SSoT runs on every trigger resolution). `model`, `phase` (`""` when none), `resource_bound_bytes`, `resource_bound_tokens`, `effective_trigger` |

`usage_source` says where the token counts came from: `provider` (the provider
reported them) or `estimated` (the provider's stream carried no usage, so
LiteLLM filled the counts locally with its own tokenizer). `unknown` means the
origin was not stated. An estimated figure is recorded and enforced exactly like
a reported one — the field exists so a cost audit can tell them apart, and so
the turns billed on an estimate can be found afterwards:

```bash
reyn events .reyn/events --filter llm_response_received   # then grep/jq for "estimated"
```

See [reference/config/budget.md](../config/budget.md) — "Token-count provenance".

## Control IR

Each Control IR op kind emits its own event:

| Kind | When |
|------|------|
| `read_file`, `write_file`, `edit_file`, `delete_file`, `glob_files`, `grep`, `regenerate_index` | `file` op variants — all via `tool_executed` with `op=<sub_op>` |
| `sandboxed_exec_started`, `sandboxed_exec_completed` | `sandboxed_exec` op — `started`: `argv`, `argv0_resolved`, `backend`; `completed`: `argv`, `argv0_resolved`, `backend`, `returncode`, `denial_class`. `argv0_resolved` (#2820) is the absolute path actually executed: a version-manager shim (`~/.pyenv/shims/python3`) is resolved to its real binary by reading the manager's on-disk layout (part A — filesystem-only, no subprocess) so the sandbox runs the real binary directly instead of the shim, whose launch-`fork()` would die under `(deny process-fork)`; equals the plain PATH resolution for a non-shim command, or the unchanged `argv[0]` when resolution is unavailable (fail-open). `denial_class` is `"fork_denied"` when the sandbox blocked `fork()` at a PATH launcher/shim (pyenv/asdf/mise/npx/uvx) under `(deny process-fork)`, else `null` — an environment/config condition, not a tool failure |
| `mcp_called`, `mcp_completed`, `mcp_failed`, `mcp_cancelled` | MCP tool ops — `mcp_cancelled` (#2813) fires instead of `mcp_completed`/`mcp_failed` when a Ctrl-C `cancel_event` interrupts an in-flight call before it completes |
| `mcp_server_installed` | `mcp_install` op — `name`, key names only (no values) |
| `mcp_install_cancelled`, `mcp_prompt_get_cancelled`, `mcp_resource_read_cancelled`, `mcp_resource_subscribe_cancelled`, `mcp_resource_unsubscribe_cancelled` | #2813 — a Ctrl-C `cancel_event` interrupted the corresponding op (install probe / get-prompt / read-resource / subscribe / unsubscribe) before it completed; the op returns `status:"cancelled"` and nothing is committed |
| `plugin_install_started`, `plugin_install_copied`, `plugin_install_registered`, `plugin_install_completed` | `plugin_install` op's own main-flow milestones — `started`: `name`, `source_kind`; `copied`: `name`, `plugin_root`; `registered`: `name`, `registered` (the per-capability-kind registration result); `completed`: `name` |
| `mcp_server_install_skipped` | `plugin_install` op (#4580) — fires once per declared MCP server the op's probe-then-commit loop drops (never a silent `continue`): `server_id`, `server_name`, `reason` (`"probe_failed"` or `"permission_denied"`), `source` (`"plugin_install:<plugin_name>"`). The op's own return value also carries a `skipped` list alongside `registered` — this event is the audit-trail counterpart, not a duplicate of it. |
| `plugin_install_reconciled` | Startup crash-recovery, not the main install flow — a partial install left behind by a prior crash is rolled back before any op runs; `name`, `action` (`"rolled_back"` today, the only value emitted) |
| `web_search_started`, `web_search_completed`, `web_search_failed` | web_search ops — `started`: `query`, `backend`; `completed`: adds `result_count`; `failed`: adds `error` |
| `web_fetch_started`, `web_fetch_completed`, `web_fetch_failed` | web_fetch ops — `started`: `url`; `completed`: `url`, `status_code`, `content_length`, `extractor`; `failed`: `url`, `status` (`"timeout"` or `"error"`), `error` |
| `semantic_search_embed_failed` | `semantic_search` op (FP-0057 Phase 2a; renamed from `recall_embed_failed`) — emitted when a model group's embed call fails; `query`, `model`, `error` |
| `index_dropped` | `index_drop` op — `source`, `chunks_dropped: int` |
| `index_update_cost_warning` | `index_update` op (FP-0057 Phase 2a) — the to-embed batch exceeds `embedding.cost_warn_threshold`; `source`, `chunk_count`, `estimated_tokens`, `threshold` |
| `index_updated` | `index_update` op — `source`, `added: int`, `updated: int`, `removed: int`, `skipped: int` |
| `control_ir_skipped`, `control_ir_failed` | dispatch failures (`control_ir_skipped` reasons include `handler_not_implemented`) |
| `permission_denied` | When an op is denied by the resolver |

## MCP

Unlike the Control IR `mcp_*` events above (tied to a tool-call op), these
fire asynchronously from the MCP connection/receive-loop, independent of any
op dispatch:

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `mcp_initialized` | Emitted on every (re)connect, once the server's handshake completes (`initialize` or, since #3698 PR-2's `Client(mode="auto")`, `discover`). | `server`, `negotiated_version`, `capabilities`, `subscription_adapter` (the selected `SubscriptionAdapter` class name — `LegacySubscriptionAdapter` or `ListenSubscriptionAdapter`, see [Concepts: MCP](../../concepts/tools-integrations/mcp.md) — the audit-visible witness of which delivery mechanism this connection actually uses) |
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
| `routing_decided` | Emitted at the router's single dispatch chokepoint (`RouterLoop._dispatch_resolved`) whenever a catalog action is dispatched — via the `invoke_action` wrapper, an ARS-salvaged direct call, or (#3455) the flat bare-name dispatch path used when `tool_use.universal_wrappers_enabled: false` (moved from `action_retrieval:`, #4552 PR-3+4) is set in reyn.yaml. | `action_name: str`; `source: str` — `"invoke_action"` \| `"ars_direct"`; `outcome: str` — `"success"` \| `"error"`; `chain_id: str` — request chain identifier for cross-call correlation. |

**Notes:** enables auditing catalog-action routing regardless of which entry surface the model used (#3455: previously gated on the `invoke_action` wrapper surface, so the `universal_wrappers_enabled: false` opt-out configuration never emitted this event at all). Cross-correlate with `chain_id` across the action's downstream events.

## User interaction

| Kind | When |
|------|------|
| `user_message_received` | A new user turn enters the runtime. Carries `chain_id` (the uuid minted by `submit_user_text` and propagated through any agent-to-agent messages this turn produces) |
| `user_intervention_received` | An `ask_user` op got its answer |
| `chat_started`, `chat_stopped` | Chat session lifecycle |
| `turn_cancelled` | A user turn was cancelled mid-router-loop (e.g. `/cancel` or a new submission supersedes the running turn). Payload: `chain_id`. |

## Session and turn lifecycle

The boundary events every trigger passes through, whatever surface submitted it.
Required fields are declared in `src/reyn/core/events/event_schema.py`.

| Kind | When | Key payload |
|------|------|-------------|
| `session_started`, `session_completed` | The session's resource scope opens / closes (alongside `chat_started` / `chat_stopped`). | `agent_name` |
| `turn_started` | A trigger has been consumed from the inbox and is about to be dispatched. | `kind` — the inbox message kind that triggered this turn, so a subscriber can tell human triggers from automated ones without parsing the payload. The vocabulary is CLOSED and enumerated in `TurnOrigin` (`src/reyn/runtime/turn_origin.py`) — read the member values there rather than a list here, which is how `"task_ready"` outlived the task system in this row. #3595 made `kind == "user"` TRUE rather than approximate: a pipeline agent step's prompt (step 1), a webhook / MCP / A2A / cron push (step 1b), and the attached-pipeline run nudge (S2) each used to arrive claiming it; each now carries its own member, so `"user"` means an operator typed the line at a first-party client and nothing else does. `chain_id`; `seq` |
| `turn_completed` | The router loop reached a terminal condition — router path only. | `chain_id` |
| `turn_settled` | The turn is done, for EVERY turn kind including slash / intervention short-circuits that return before the router. The reliable clear signal for a working indicator started on `turn_started`. | `kind`; `chain_id` (may be absent for non-user triggers) |
| `project_context_changed` | Turn boundary (`ProjectContextWatcher.check`, `src/reyn/runtime/project_context_watch.py`, #3787): the project context file's (`AGENTS.md` / `REYN.md`) mtime differs from what was last observed. Fires once per edit, never repeats until the file changes again. **Detection only** — the live `project_context` a session's system prompt was built with is NOT reloaded; the file is fenced as untrusted operator data before it reaches the prompt (`RouterHostAdapter.get_project_context`), so re-reading it is deliberately not wired through the LLM-writable hot-reload IN-set. What to do with a detected change (surface-only vs. actually reload) is an open decision, not yet made. | `path` |

## Tool dispatch

Emitted by `dispatch_tool` (`src/reyn/core/dispatch/dispatcher.py`) around every
router-dispatched tool call. `tool_returned` and `tool_failed` are mutually
exclusive per call.

| Kind | When | Key payload |
|------|------|-------------|
| `tool_called` | Before invocation, after argument validation. | `caller_kind`, `caller_id`, `tool`, `chain_id`, `args`, `args_hash` |
| `tool_returned` | The invocation returned a value that does NOT declare an error (see `tool_failed`). | `caller_kind`, `caller_id`, `tool`, `chain_id`, `args_hash`, `result` |
| `tool_failed` | The invocation was refused, raised, **or returned normally with a self-declared error** (#3450 — a handler's own `{"error": ...}` / `{"error_message": ...}` / `{"error_kind": ...}` return, plain or one level under its own `{"status": "error", "data": {...}}` self-envelope, promoted to this event instead of silently wrapped as a success). | same, plus `error_kind` (`permission_denied` \| `exception` \| a validation reason \| a handler-supplied kind \| `handler_error`) and `message` |

`args_hash` is a stable SHA-256 prefix over the canonical-JSON arguments — the
correlation id that pairs a `tool_called` with its outcome across the log.

**Remote fan-out.** `turn_started` / `llm_called` / `tool_returned` / `tool_failed`
are the kinds the A2A and MCP progress surfaces forward to a remote peer
(`src/reyn/core/events/progress_lifecycle.py`); see
[Concepts: A2A § Progress payloads](../../concepts/multi-agent/a2a.md#progress-payloads)
and [Concepts: MCP § Progress notifications](../../concepts/tools-integrations/mcp.md#progress-notifications).
Three of the four (`turn_started`, `tool_returned`, `tool_failed`) are also what
the AG-UI transport maps to `RUN_STARTED` / `TOOL_CALL_END` — see
[agui-transport.md](agui-transport.md) § "Working-indicator path". Cost events
(`llm_called`) ride the progress fan-out but not the AG-UI working-indicator set.

## Agent-to-agent messaging

| Kind | When | Key payload |
|------|------|-------------|
| `agent_message_sent` | `_send_to_agent` or `_send_agent_response` delivered a payload | `kind=agent_request\|agent_response`, `from_agent`, `to_agent`, `depth`, `chain_id` |
| `agent_request_received` | Receiving agent pulled an `agent_request` from its inbox | `from_agent`, `depth`, `chain_id` |
| `agent_response_received` | Originating agent pulled an `agent_response` from its inbox | `from_agent`, `depth`, `chain_id` |
| `agent_message_refused` | A send was refused (e.g. exceeded `safety.loop.max_agent_hops`) | `reason`, `to_agent`, `depth`, `chain_id` |
| `chain_timeout` | A pending chain exceeded `safety.timeout.chain_seconds` and was force-resolved with a synthetic error response upstream | `chain_id`, `waiting_on` (sorted list of agents that hadn't replied), `timeout_seconds`, `origin_agent` |
| `task_settle_undelivered` | A task's settle disposition executed, but its reply target `(agent, sid)` is no longer resolvable (agent removed / session not loaded) — the settle still goes terminal; delivery is dropped, fail-safe, NEVER rerouted to `main` (proposal 0067 P9, #3978) | `run_id`, `reply_to_agent`, `reply_to_sid`, `reason` |

`chain_id` is uuid4 hex; one per top-level user submission, propagated unchanged across every hop. Cross-agent reconstruction is `grep <chain_id>` over each agent's `events.jsonl` plus `history.jsonl`.

## Workspace

| Kind | When |
|------|------|
| `workspace_updated` | Any artifact is written |
| `tool_executed` | Generic tool dispatch |

## Tool-result canonicalization

Every tool/op result is normalized to one canonical shape before it reaches the LLM. A producer with a real mapper is shaped cleanly; one that has no mapper (declared debt, or a genuinely unregistered source) takes a lossless whole-dict fallback instead — and that fallback is made visible here rather than silent, so unmapped-producer debt is one `grep` away.

| Kind | When | Key payload |
|------|------|-------------|
| `canonical_fallback_used` | A tool/op result took the whole-dict canonical fallback: a producer with declared-but-unwritten mapper debt, a genuinely unregistered / unknown source, or a passthrough producer whose whole-dict blob exceeded the structured offload gate. | `source` — the invoked producer id (op kind / tool name; `null` for a genuine unknown); `reason` — `unregistered` \| `canonical_todo` \| `passthrough_oversized`. Carries the source identity only — never the result body. |

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
| `compaction_batch_cap_below_head_tail_budget` | #4477 — `resolve_effective_trigger_and_budgets`'s 4th resource/budget invariant instance (`src/reyn/runtime/services/router_history_buffer.py`, siblings to `resource_cap_exceeds_budget_trigger` below): the compaction batch read's own byte cap (`history_tail_reader.COMPACTION_BATCH_MAX_BYTES`, 8 MiB) is smaller than `head_budget + tail_budget`'s own combined token footprint converted to bytes via `context_builder.INLINE_CAP_BYTES_PER_TOKEN` — meaning a compaction pass would produce zero candidates every time (head+tail trimming alone consumes the whole batch), permanently stalling `covers_through_seq`. Confirmed reachable against real, currently-cataloged litellm models (>8 MiB combined at the shipped `component_weights` default). Detection only — no value is clamped. Warn-once per `(model, phase)`. `model`, `phase` (`""` when none), `head_budget`, `tail_budget`, `combined_bytes`, `compaction_batch_max_bytes` |
| `compaction_check` | The compaction gate ran for a turn. `outcome` records the decision — e.g. `too_few_turns`, `below_min_batch`, `pre_frame_overflow`, `already_running`, `forced_sync`, `forced_sync_no_turns`, `compaction_input_gap_invariant_violated` (#4472: candidates are read directly from the durable `history.jsonl`, never residency-gated, so this should never fire under normal operation — a defensive invariant, not a routine branch; a hit means something else narrowed the durable read out from under compaction, reopening #4470's silent-coverage-claim defect through a new path). `outcome="forced_sync"` also carries `batch_truncated` (#4472: `True` when the durable read's own per-call byte cap capped this pass short of the full uncovered range — the pass still only claims coverage of what it actually examined; a large backlog needs multiple passes, each with `batch_truncated=True` until the final one). Some outcomes also carry `turns`, `head`, `tail`. | `outcome`, plus outcome-specific fields |
| `compaction_failed` | A compaction attempt raised. | `error` |
| `compaction_shrink_recovered` | (#3783 stage 2) `retry_loop`'s bounded shrink ladder caught a context-overflow (compaction-call or main-call) and is about to shrink and retry. Fires once per recovered iteration, not once per turn — a turn that shrinks 3 times emits 3 of these. | `cause` (the caught exception's class name), `iteration` (0-based ladder position), `consecutive` (how many times in a row `cause` has recovered without a different cause or a success in between) |
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
