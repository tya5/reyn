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
compaction_schema_invalid
compaction_shrink_recovered
compaction_started
compaction_wire_bytes_measured
composer_dropped
composer_fired
config_reload_rejected
config_reloaded
control_ir_failed
control_ir_skipped
cron_fired
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
file_changed
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
mcp_media_denied
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
pipeline_install_skipped
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
plugin_install_token_vocabulary_mismatch
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
skill_body_threat_blocked
skill_body_threat_match
skill_install_skipped
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
webhook_received
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

**An AUDIT-event's body field is `data`, read `e["data"][...]`, not
`e["payload"]`.** For a `llm_response_received` event, `e["data"]["finish_reason"]`
/ `e["data"]["call_id"]` are where those fields live; for `tool_called`,
`e["data"]["caller_kind"]` / `e["data"]["caller_id"]` / `e["data"]["tool"]` /
`e["data"]["args"]`. `e.get("payload", {})` returns `{}` for EVERY audit-event,
silently — every field reads as absent, not because it wasn't emitted, but
because the read itself targeted a key that was never on THIS vocabulary.
`payload` is a real key, but on the WAL-event's own vocabulary (`.reyn/state/wal.jsonl`
— see [Time-Travel § WAL vs audit-event separation](../../concepts/runtime/time-travel.md#wal-vs-audit-event-separation),
and CLAUDE.md's "'event' is three distinct things"): `agent_snapshot.py`'s
`inbox_put` replay reads `event.get("payload", {})` for real. Mixing the two
vocabularies returns empty in EITHER direction — an audit-event read as
`e["payload"]`, or (the mirror mistake) a WAL-event read as `e["data"]`.

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
| `llm_response_received` | `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_creation_tokens`, `cost_usd`, `usage_source` (+ `chain_id`). `call_id`/`finish_reason` (#4691 Phase 1 ①) are the litellm `ModelResponse`'s own `id`/`choices[0].finish_reason`, measured off the response and stamped ONLY here (not `llm_called`, which fires before the response exists) — the call-granularity key the TUI outbox meta and the flowview tree (#4691 Phase B) use to tell which litellm call a row belongs to and whether it was a tool round (`finish_reason == "tool_calls"`) or the turn's own reply (`"stop"`). `None` when genuinely absent off the response — never a minted placeholder, and this includes litellm's own `""` id: the stamp point sits at `_once`'s exit, common to both the sync and streaming call paths — streaming reconstructs a `ModelResponse` via litellm's `stream_chunk_builder`, and `.id` is present there too (lead-coder measurement, #4722) — but litellm's `ChunkProcessor._get_chunk_id` returns `""`, not `None`, when no stream chunk carried an id, and `ModelResponse.__init__` only mints a fresh id when the field is `None`, so a genuinely-absent streamed id survives onto the response as `""`. Reyn's own stamp collapses that (and any other falsy id) to `None` before emitting, so this field is never `""` on the event — a missing id reads as absence, not as a shared empty key multiple unrelated calls would otherwise collide on. |
| `embedding_index_build_complete` | `source_id`, `chunk_count`, `total_tokens`, `cost_usd`, `embedding_model` (a disk-adopt/no-fresh-build completion carries `total_tokens`/`cost_usd` as `null` — no embed call happened that run, not a cost of zero) |
| `repo_ingest_files_skipped` | #4431 — the repo-knowledge (`knowledge_repo_doc`/`knowledge_repo_src`) background build excluded one or more files for exceeding `_REPO_INGEST_MAX_BYTES` (256 KB, `src/reyn/data/index/knowledge_ingest.py`). Emitted once per build, only when the count is nonzero — a routine build with nothing skipped emits no event at all. `kind` (`"doc"`/`"src"`), `skipped_count`, `reason` (currently always `"over_size_cap"`) |
| `resource_cap_exceeds_budget_trigger` | #4381 PR-1 — `resolve_effective_trigger_and_budgets`'s own invariant check (`src/reyn/runtime/services/router_history_buffer.py`): the per-result resource boundary (`control_ir_inline_cap`, bytes, converted to tokens via `context_builder.INLINE_CAP_BYTES_PER_TOKEN`) exceeds the budget boundary (`effective_trigger`, the model's context window). Detection only — no value is clamped. Warn-once per `(model, phase)`, not per call (this SSoT runs on every trigger resolution). `model`, `phase` (`""` when none), `resource_bound_bytes`, `resource_bound_tokens`, `effective_trigger` |
| `model_budget_fallback` | `get_max_input_tokens` (`src/reyn/llm/model_budget.py`) could not resolve a real context-window figure for `model` and used the conservative 128,000-token default instead. Warn-once per model string (process-shared). #4680②: `reason` distinguishes WHY — `"not_ready"` (litellm has not finished importing in this process yet; TEMPORARY, self-corrects once litellm is ready — see this same call site's own log for the correction, which is log-only, not a second event) vs `"uncataloged"` (litellm IS loaded but has no catalog entry, or no positive `max_input_tokens`, for this exact model string; PERMANENT for this process unless `llm.models.<tier>.max_input_tokens` is configured, #4689, or litellm's own catalog is updated). Before #4680②, both states fired this SAME event with no way to tell them apart. `model`, `fallback_tokens`, `reason`, `phase` (`None` when none), `run_id` |

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
| `mcp_media_denied` | #4946 — an MCP tool response carried an image whose byte size the multi-modal gate (`require_media_load`, same shared gate as `file_read_media_denied`/`web_fetch_media_denied`) rejected under `multimodal.on_oversize=deny` (or an `ask` prompt the operator declined). Gated PER IMAGE, not per call — an MCP tool can return several images in one response, and one oversized image is dropped (replaced with a text denial note) without discarding the rest of the result. `server`, `tool`, `size_bytes`, `mime_type` |
| `mcp_server_installed` | `mcp_install` op — `name`, key names only (no values) |
| `mcp_install_cancelled`, `mcp_prompt_get_cancelled`, `mcp_resource_read_cancelled`, `mcp_resource_subscribe_cancelled`, `mcp_resource_unsubscribe_cancelled` | #2813 — a Ctrl-C `cancel_event` interrupted the corresponding op (install probe / get-prompt / read-resource / subscribe / unsubscribe) before it completed; the op returns `status:"cancelled"` and nothing is committed |
| `plugin_install_started`, `plugin_install_copied`, `plugin_install_registered`, `plugin_install_completed` | `plugin_install` op's own main-flow milestones — `started`: `name`, `source_kind`; `copied`: `name`, `plugin_root`; `registered`: `name`, `registered` (the per-capability-kind registration result); `completed`: `name` |
| `mcp_server_install_skipped` | `plugin_install` op (#4580) — fires once per declared MCP server the op's probe-then-commit loop drops (never a silent `continue`): `server_id`, `server_name`, `reason` (`"probe_failed"` or `"permission_denied"`), `source` (`"plugin_install:<plugin_name>"`). The op's own return value also carries a `skipped` list alongside `registered` — this event is the audit-trail counterpart, not a duplicate of it. |
| `pipeline_install_skipped`, `skill_install_skipped` | `plugin_install` op (#4590) — fires once per declared pipeline/skill whose own sub-install call (`pipeline_install`/`skill_install`) returned a non-`"installed"` status instead of raising (a bad name, a threat-scan block, a missing DSL file, ...): `plugin_id`, `path` (the pipeline DSL file / skill directory), `reason` (the sub-install's own `status` value — e.g. `"error"`, `"blocked"`), `error` (the sub-install's own `error` message, `""` if absent). Unlike mcp's probe-then-commit (which skips BEFORE ever calling the sub-install), a pipeline/skill sub-install always runs; the skip is read from its return value, not a separate pre-check. |
| `skill_body_threat_match`, `skill_body_threat_blocked` | `load_skill` op (#4699) — the body-scan chokepoint every skill body crosses regardless of how its entry was registered (`skill_install`'s own `skill_install_threat_match`/`skill_install_threat_blocked` above cover only the frontmatter `description` and only the `skill_install` op, so a hand-written `skills.yaml` entry never reaches those). `match` fires once per pattern hit (`pattern_id`, `severity`, `scope`); `blocked` fires when a hit reaches `block_severity` (`pattern_id`, `severity`, `path`) — the load returns `status: "blocked"` with `content: ""`, so the body never reaches the model's context. |
| `plugin_install_token_vocabulary_mismatch` | `plugin_install` op (#4610) — fires once per finding in `_expand_plugin_files`'s own `stale_token_warnings` list (mcp.json's `args`/`env`/`cwd` still containing `${REYN_PLUGIN_ROOT}`/`${REYN_PROJECT_DIR}`/`${REYN_SKILL_DIR}`, or a `pipelines/*.yaml`/`skills/*/SKILL.md` file still containing `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` — the OTHER file's token vocabulary, never expanded where it was found so it survives literally with no error otherwise): `name`, `warning` (the same human-readable string the op's own `stale_token_warnings` return-value list carries — this event is the durable audit-trail counterpart, so a plugin installed once and never re-inspected still has a record). `mcp.json`'s `command`/`url` fields never trigger this — the Agent Plugins 1.0 spec deliberately never expands tokens there, so a literal token surviving in either field is correct, not a mismatch. Report-only: never blocks the install, never rewrites the plugin's files. |
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

## External events

The 4 points a hook's `on:` can subscribe to as an external-event source —
see [Concepts: hooks § External-event points](../../concepts/runtime/hooks.md#external-event-points).
`mcp_resource_updated` (above, MCP section) is the one of the 4 that has
always emitted an arrival audit event; #4605 closed the same gap for the
remaining 3 — each now emits its own arrival event REGARDLESS of whether a
hook is configured to consume it, so the signal's arrival is reconstructable
from `.reyn/events` even when nothing was listening.

| Kind | Trigger | Key payload |
|------|---------|-------------|
| `file_changed` | A watched path (`fs_watch.paths`) reports a create/modify/delete via the `watchdog` observer, after debounce (`fs_watch.debounce_seconds`) and symlink-path rewrite. See [Concepts: hooks § file_changed](../../concepts/runtime/hooks.md#file_changed). | `path` (rewritten to the operator-configured prefix), `event_type` (`"created"` \| `"modified"` \| `"deleted"`) |
| `cron_fired` | A scheduled cron job fires, on the job's own resolved `cron:<job_name>` session, right after session resolution. See [Concepts: hooks § cron_fired](../../concepts/runtime/hooks.md#cron_fired). | `job_name`, `to` (operator-authored config, never end-user-supplied) |
| `webhook_received` | An inbound webhook (slack/line/generic plugin) is routed to its resolved per-sender session, right after session resolution. See [Concepts: hooks § webhook_received](../../concepts/runtime/hooks.md#webhook_received). | `transport`, `sender` — NEVER the raw inbound body/text, which may carry tokens/PII (same discipline as `hook_push_fired`'s "never the message body") |

All three are best-effort: a sink fault logs and is swallowed, never blocking
the job's inbox delivery / the webhook's HTTP response / the watcher's drain
loop.

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
| `agent_delta` | One streamed content chunk during an LLM reply (#3288 ③b). Live subscriber delivery (TUI / AG-UI) fires for EVERY fragment, unthrottled. #4960 (architect ruling C): the DURABLE write side is different — `LocalEventBackend` coalesces to one record per `audit_events.agent_delta_coalesce_fragments` fragments (default 100) or `audit_events.agent_delta_coalesce_interval_ms` milliseconds (default 2000), whichever comes first, per `chain_id`, plus one final record when the stream ends (success, exception, or cancellation — `RouterLoop.run()`'s own `finally`). Measured (2000-delta/60KB real streamed reply): unthrottled, `agent_delta` was 99.4% of the audit file's total bytes for that run. `reyn events replay` therefore sees FEWER `agent_delta` records than fragments actually streamed — declared via `LocalEventBackend.declare_gaps()` (contract 2), not a silent loss. A record produced by coalescing carries `coalesced_fragment_count` (the raw event schema otherwise has none of these fields). | `text`, `chain_id`, `round_index`, `coalesced_fragment_count` (present only on a coalesced/durable record, absent on the live-dispatch-only fragments it stands in for) |
| `turn_settled` | The turn is done, for EVERY turn kind including slash / intervention short-circuits that return before the router. The reliable clear signal for a working indicator started on `turn_started`. | `kind`; `chain_id` (may be absent for non-user triggers) |
| `project_context_changed` | Turn boundary, emitted by TWO `ProjectContextWatcher` instances per session (`src/reyn/runtime/project_context_watch.py`, #3787/#4263) — tell them apart by `path`. **Project-wide file** (`project_context_path` → `AGENTS.md` / `REYN.md`): **detection only** — the file is scanned, not fenced (#4830 — operator/agent-editable content, the same trust class as Claude Code's CLAUDE.md, backstopped by the file-write permission gate rather than a per-turn fence marker), but the live `project_context` a session's system prompt was built with is frozen at construction and NOT reloaded from this signal (owner ruling, #3787: this file is read once at startup by design, not hot). **Per-agent file** (`.reyn/agents/<agent_name>/AGENTS.md`, owner ruling B, #3787): DOES reload — but not because of this event. `RouterHostAdapter.get_project_context` reads this file fresh on every call regardless (it is already invoked live every turn, never from a memoized value), so an edit is reflected on the very next turn with no dependency on this watcher firing at all; this event is purely the audit-event signal that an edit was *observed*, for the same reason every other `*_changed` kind exists (band: observability). Either way: fires once per edit, never repeats until the file changes again (mtime comparison, not content hash — same content re-touched, e.g. a branch switch, can still fire once). | `path` |

## Tool dispatch

Emitted by `dispatch_tool` (`src/reyn/core/dispatch/dispatcher.py`) around every
router-dispatched tool call. `tool_returned` and `tool_failed` are mutually
exclusive per call.

| Kind | When | Key payload |
|------|------|-------------|
| `tool_called` | Before invocation, after argument validation. | `caller_kind`, `caller_id`, `tool`, `chain_id`, `call_id`, `args`, `args_hash` |
| `tool_returned` | The invocation returned a value that does NOT declare an error (see `tool_failed`). | `caller_kind`, `caller_id`, `tool`, `chain_id`, `call_id`, `args_hash`, `result` |
| `tool_failed` | The invocation was refused, raised, **or returned normally with a self-declared error** (#3450 — a handler's own `{"error": ...}` / `{"error_message": ...}` / `{"error_kind": ...}` return, plain or one level under its own `{"status": "error", "data": {...}}` self-envelope, promoted to this event instead of silently wrapped as a success). | same, plus `error_kind` (`permission_denied` \| `exception` \| a validation reason \| a handler-supplied kind \| `handler_error`) and `message` |

`call_id` (#4691 Phase B ①, remainder — `DispatchContext.call_id`) is the
litellm call (`LLMToolCallResult.call_id`, #4725) whose `tool_calls` this
dispatch belongs to — the SAME key `llm_response_received` carries for that
call (#4722). `None` for a dispatch whose `DispatchContext` never threaded one
through (the router's own tool-turn dispatch always does — `RouterLoop`
reads it off the current round's `LLMToolCallResult.call_id` and passes it
as an explicit parameter down `dispatch()`/`_run_execute_round()`/
`_dispatch_resolved()`, never a stored field, so there is nothing that can go
stale between rounds; an op-loop or other non-router caller that constructs
its own `DispatchContext` may still leave it unset) — never a
minted placeholder. This is the key a TUI consumer uses to attach a tool row
to its parent CALL (#4691 Phase B's flowview tree); `chain_id` alone cannot
do this because one turn's `chain_id` can span several litellm calls, and
dispatch order is explicitly NOT used for this (owner ruling B, #4691: order
holds only while every reader reconstructs it identically, is one of several
independent invariants, and a single broken one goes unnoticed silently).

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
| `compaction_started` | A compaction pass begins (`CompactionController`) — the LLM call that follows is about to spend real tokens and rewrite context. | `new_turn_count`, `covers_through_seq`, `had_previous` |
| `compaction_completed` | A compaction pass finished — the rolling summary was written to history. `prompt_tokens`/`completion_tokens`/`cost_usd` (#4703 axis①) are the compact()-call's OWN real usage — the primary compaction LLM call only, not any `summary_resummarize` follow-up pass (a rare, bounded-to-1-by-default backstop; that pass's cost is a disclosed, separate gap, not folded in here). `None` for any of the three only when usage genuinely could not be read off the response — never coerced to `0`. This is what `ChatLifecycleForwarder.on_compaction_completed` reads to put real money on the `[↑ N turns compacted · ↑8.2k ↓340 · $0.05]` conversation-face marker — before #4703 this row existed but never showed the spend. | `new_turn_count`, `covers_through_seq`, `section_lengths`, `prompt_tokens`, `completion_tokens`, `cost_usd` |
| `compaction_batch_cap_below_head_tail_budget` | #4477 — `resolve_effective_trigger_and_budgets`'s 4th resource/budget invariant instance (`src/reyn/runtime/services/router_history_buffer.py`, siblings to `resource_cap_exceeds_budget_trigger` below): the compaction batch read's own byte cap (`history_tail_reader.COMPACTION_BATCH_MAX_BYTES`, 8 MiB) is smaller than `head_budget + tail_budget`'s own combined token footprint converted to bytes via `context_builder.INLINE_CAP_BYTES_PER_TOKEN` — meaning a compaction pass would produce zero candidates every time (head+tail trimming alone consumes the whole batch), permanently stalling `covers_through_seq`. Confirmed reachable against real, currently-cataloged litellm models (>8 MiB combined at the shipped `component_weights` default). Detection only — no value is clamped. Warn-once per `(model, phase)`. `model`, `phase` (`""` when none), `head_budget`, `tail_budget`, `combined_bytes`, `compaction_batch_max_bytes` |
| `compaction_check` | The compaction gate ran for a turn. `outcome` records the decision — e.g. `too_few_turns`, `below_min_batch`, `pre_frame_overflow`, `already_running`, `forced_sync`, `forced_sync_no_turns`, `compaction_input_gap_invariant_violated` (#4472: candidates are read directly from the durable `history.jsonl`, never residency-gated, so this should never fire under normal operation — a defensive invariant, not a routine branch; a hit means something else narrowed the durable read out from under compaction, reopening #4470's silent-coverage-claim defect through a new path). `outcome="forced_sync"` also carries `batch_truncated` (#4472: `True` when the durable read's own per-call byte cap capped this pass short of the full uncovered range — the pass still only claims coverage of what it actually examined; a large backlog needs multiple passes, each with `batch_truncated=True` until the final one). Some outcomes also carry `turns`, `head`, `tail`. | `outcome`, plus outcome-specific fields |
| `compaction_failed` | A compaction attempt raised. | `error` |
| `compaction_schema_invalid` | #4883 — the compaction LLM response was syntactically valid JSON but had an empty/missing `topic_arc` (whose emptiness cannot be told apart from a dead response — see `_validate_chat_summary_fields`'s own docstring). Fires once per invalid attempt, including the LAST one right before `compact()` raises (`CompactionConfig.max_schema_reprompt_attempts` bounds the re-prompt budget) — so a persistent failure across the whole budget shows as N of these, then a `compaction_failed`, not a silent gap. `new_turn_seqs` no longer gates this (#4951-A) — an empty/wrong echo of that field can no longer trigger it, since reyn no longer reads the echo at all. | `attempt` (0-based), `max_attempts`, `errors` (the missing-field messages) |
| `compaction_shrink_recovered` | (#3783 stage 2) `retry_loop`'s bounded shrink ladder caught a context-overflow (compaction-call or main-call) and is about to shrink and retry. Fires once per recovered iteration, not once per turn — a turn that shrinks 3 times emits 3 of these. | `cause` (the caught exception's class name), `iteration` (0-based ladder position), `consecutive` (how many times in a row `cause` has recovered without a different cause or a success in between), `t_max_override` (#4885: `None` on an ordinary token-based recovery; the LOCAL, retry_loop-scoped T_max this specific iteration is using once an HTTP 413 — a request-BODY-BYTE limit — triggers binary-search-halving of the in-turn token ceiling, so the temporarily-lowered window is visible in the SAME audit trail rather than running silently smaller) |
| `compaction_wire_bytes_measured` | #4944① — `retry_loop` measured the wire-JSON byte size of a `main_call` request (`estimate_wire_bytes`: SP + head + summary + tail + new_msg, the byte-axis sibling of the token `estimate` already computed here). Fires on BOTH outcomes, not success only (a turn whose every attempt 413s — the real-machine shape #4944 was opened for — would otherwise emit this event zero times, leaving no diagnostic trail at all): `accepted=True` on a successful call (the real limit is >= this value, a lower bound) and `accepted=False` when the call raised an HTTP 413 specifically (the real limit is < this value, an upper bound) — the two bracket where the limit actually sits. The `accepted=False` emission is skipped for a `compact()`-originated 413 (a different, unmeasured payload — raw_middle + section_caps, not head/summary/tail/new_msg). Measurement only: nothing reads this value to make a decision yet (#4944②/③, not yet landed). | `wire_bytes` (the measured total; KNOWN under-count — excludes tools-schema bytes and provider-specific wrapper overhead, see `estimate_wire_bytes`'s own docstring), `accepted` (`True`/`False`) |
| `elide_evaluated` | `RouterHistoryBuffer.build_history()`'s own elide/no-elide decision — `total` is the estimated token size of the turns THIS METHOD is about to send (#4954(2): already excludes any turn at or below the compaction watermark, permanently, before this total is computed — see `chat-compaction.md`'s own updated Head description); `total <= effective_trigger` means the survivors are sent raw with no further trim, not (as before #4954) "the whole conversation is sent raw" — a covered turn never re-enters via this branch either. Fires once per `build_history()` call, not once per turn slice. | `total`, `effective_trigger` |
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
