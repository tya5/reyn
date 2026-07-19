# `Session.__init__` construction rationale

`Session.__init__` (`src/reyn/runtime/session.py`) wires ~40 sub-components together at
session construction time. Historically the wiring carried its design rationale —
ordering constraints, eager-vs-deferred dependency resolution, byte-identical-extraction
provenance, trade-offs — inline as long comments (#3121 step2 audit: 582 of 959 `__init__`
lines, 61%, were comments). This doc is the **relocation target** for that rationale: code
keeps a one-line intent + a pointer here (or to the originating issue, when the issue
already carries the detail); this doc keeps the *why*.

The organizing spine mirrors the codebase's own `#3082` "Family" decomposition — the
builder methods (`_build_*_bundle`) that assemble `__init__`'s ~40 sub-components — plus a
handful of topical sections for construction rationale that predates / sits outside that
decomposition (identity, capability/visibility, multimodal, safety, misc lifecycle wiring).

## Identity (the `Agent` value object) — FP-0043 Stage 2

`self._agent` is either the caller-supplied `Agent` value object, or (when `agent=None`,
the direct/test-construction path) built from the flat identity params
(`agent_name`/`model`/`permission_resolver`/`workspace_base_dir`/`workspace_state_dir`/
`sandbox_config`/`sandbox_backend`/`environment_backend`/`agent_role`). In production,
`build_scoped_chat_session` is the single chokepoint that assembles the real `Agent` and
passes it in; the fallback exists purely so tests / other direct construction sites don't
need to build one by hand — it is byte-identical to the pre-FP-0043 direct-attribute shape.

`agent_name`/`model`/`_perm`/workspace dirs/`environment_backend`/`sandbox_config`/
`sandbox_backend`/`workspace_dir`/`agent_role` are then read-only `@property` delegations
to `self._agent` (defined later in the class body) — external code reads them exactly as
before; only the storage moved.

Field-by-field notes on the identity cluster (now Agent-held, exposed via the `@property`
block):
- `_sandbox_config` — exec-tool backend policy, plumbed to spawned Agents.
- `_environment_backend` (#1200 PR-F1) — the agent's `EnvironmentBackend` INSTANCE for the
  chat FS seam (the router `Workspace` built in `make_router_op_context`); `None` → the
  workspace's own `HostBackend` default.
- `_workspace_base_dir` / `_workspace_state_dir` (#187) — the chat `OpContext`
  `Workspace`'s FS root + host-side state dir. With a container env-backend the repo lives
  *inside* the container, so `base_dir` must be the container repo root (the partner of
  `build_environment_backend`'s backend) — otherwise `file__read`/`grep`/`glob` resolve
  against the host cwd and the agent never sees the target tree (the #187 step-3 empty-FS
  defect this param closes).
- `_sandbox_backend` (#1200 PR-F2) — the agent's `SandboxBackend` INSTANCE for the chat exec
  seam (the router `OpContext`); `None` → `get_default_backend`. This is the INSTANCE, not
  the `sandbox_config.backend` STRING used for exec-tool gating — for a docker agent it is
  the SAME object as `environment_backend` (`DockerEnvironmentBackend` satisfies both
  protocols); REQUIRED in production, because without it chat falls back to
  `get_default_backend` (rebuild-per-call, no docker) — a *different* backend than the FS
  seam, i.e. a single-shared-sandbox violation.

## `#3121` step1 parameter objects

`reactivity` / `capability_scope` / `task_wiring` / `presentation_wiring` replace 12 flat
params (see `reyn.runtime.session_params` for the per-field type definitions). Each
defaults to its own all-`None` dataclass when omitted, then is unpacked into the same
local names `__init__`'s body already read — byte-identical to the pre-step1 flat-param
`None` defaults. `presentation_wiring.presentation_consumer` is REQUIRED in production
(`build_scoped_chat_session` always supplies one); `None` is reachable only via
direct/test construction.

## Family 1 — Audit-event spine (P6)

`_build_audit_event_bundle` constructs `event_store` → `chat_events` (`EventLog`) →
`outbox_hub`, plus the opt-in OTEL subscriber attached to `chat_events` when an OTLP
endpoint is configured (P5 ADR-0039: config value or `OTEL_EXPORTER_OTLP_ENDPOINT` env).
`None`/no-endpoint → not attached, zero overhead, byte-identical to no-OTEL. This is
Family 1 because several later families (3, 4, 6a, 8a) eagerly read `self._chat_events`
at construction and must run *after* it — the ordering constraint that pins Family 1 to
run before Families 3/4/6a/8a below.

`self._chat_events` is also published as the process's ambient `EventLog` sink for the LLM
`acompletion` chokepoint (#1669: `set_llm_request_event_log`) — every in-session LLM call
emits an observable `llm_request` event without threading events through the call stack.

## Family 2 — Recovery (WAL / journal)

`_build_recovery_bundle` constructs `generation_store` → `journal` (`SnapshotJournal`,
extracted in PR-refactor-session-1 wave 2 — the session keeps `_snapshot_path` only for
diagnostic logging; the journal owns the actual I/O). The builder reads the *local*
`state_log` parameter, not `self._state_log`. `state_log` is process-shared (owned by
`AgentRegistry`); `None` disables persistence (tests / non-chat invocation). `_session_id`
(FP-0043 Stage 5, default `"main"`) threads to the journal so every WAL append carries it;
a spawned session's real sid is set post-construction (`spawn_session` → `set_session_id`)
before its run-loop goes live, so every append carries the right `session_id` for
per-session snapshot routing.

Adjacent recovery-adjacent state that stays inline (not builder-owned):
- `_turn_idle` (ADR-0038 Stage 1c) — set = no turn in flight; cleared while
  `run_one_iteration` processes a turn. Lets a global rewind `await_quiescent` before
  appending the reset-record, so no append lands past the reset seq.
- `_turn_owner_task` — lets `await_quiescent` skip `_turn_idle.wait()` when called
  re-entrantly from the same task that owns the current turn (e.g. a slash handler calling
  `registry.checkout` mid-turn).
- `_inflight_wal_tasks` (ADR-0038 Stage 1c coverage) — joinable handle for fire-and-forget
  WAL-append tasks (intervention dispatch / `intervention_answer_consumed`) that would
  otherwise escape `await_quiescent`; each spawn registers via `_track_wal_task`, and
  `await_quiescent` joins the set so no such append can land past the rewind reset-record
  seq (discard-on-done keeps it bounded).
- `_state_log` is also kept directly (not only via the journal) because ops launched from
  this session need it to emit step events into the same WAL that the journal writes to.
- `_halted_reason` (#2259 PR-3) — set when the session FAIL-STOPS (e.g.
  `"durability_failure"`); `None` while running. In-memory only (durability is dead → it
  cannot itself be a durable event) — the operator-visible pair to the raised
  `DurabilityHaltError`.

## Family 3 — Hook-event / reactivity

`_build_hook_event_bundle` constructs `hook_bus` → the awaited `hook_dispatcher` →
`fs_watcher` → `composer_registry` → `composed_consumer` → `hot_reloader` together. It runs
right after Family 1's `chat_events` assignment because this family *consumes*
`chat_events`: `hot_reloader` reads it EAGERLY at construction (`events=chat_events`), and
`hook_bus`/`hook_dispatcher`/the Composers emit through deferred `self._chat_events`
lambdas.

The config-derivation this builder takes as inputs is resolved inline, BEFORE the builder
call:
- Hooks are LAYERED (#2073 S2b): the `reyn.yaml` startup layer (OUT-set, captured once as
  `_startup_hooks_raw`, never re-read on reload) ∪ the `.reyn/hooks.yaml` runtime layer
  (IN-set, hot-reloadable; the LLM-op writes it in S3). `_build_hook_registry` combines
  them; the boot registry includes the runtime layer too (active from session start,
  mirroring `.reyn/mcp.yaml`), and the hooks-reapply seam re-reads only the runtime layer +
  re-combines.
- Composers mirror the same layering (Hook-Event Redesign Phase 4b/5, #2880/#2881):
  `_startup_composers_raw` captures the `composers:` startup (OUT-set) layer once;
  `_build_composer_defs` combines it with the runtime/per-agent/per-session layers (same
  4-layer additive shape as `_build_hook_registry`). Composers are v1-startup-only — no
  hot-reload/reapply seam, unlike hooks (restarting a live Composer's `PendingStore`
  mid-session is a separate, not-yet-designed concern).
- `_build_composer_defs` is deliberately run BEFORE `_build_hook_registry`: it is a
  pure/side-effect-free parse (confirmed — no hook-registry interaction), so knowing the
  full set of configured composers (all 4 layers) BEFORE hooks are validated lets a
  `composed:*` hook's `matcher` be schema-checked too (#2889) — closing the open-set gap
  Phase 3 left for composed kinds (every composed event, across all 7 Composer ops, is
  emitted by the single `_emit_composed` producer with the fixed payload shape
  `{"inputs": [...], "correlation_key": <key>}` — `composer.py:336-338` — so this schema is
  knowable and identical for every composer, keyed by its `emit_kind`). Composers are
  v1-startup-only, so `self._composed_schemas` is computed once here and reused by the
  hooks-reapply seam too.
- `_runtime_cron_names` (#2073 S4) tracks the RUNTIME (`.reyn/cron.yaml`) cron job names so
  the cron-reapply seam can unschedule jobs removed from the runtime file WITHOUT touching
  startup (`reyn.yaml`) jobs (the same startup/runtime layering as hooks); seeded from the
  boot IN-set, updated each reload.
- `_fs_watch_cfg` (#2608 H4 / #3082 Family 3) is a precursor/builder input resolved here;
  the `FsWatcher` itself is constructed inside `_build_hook_event_bundle` alongside the
  rest of the hook-event family. `paths`/`debounce_seconds` default to empty/`0.2` when no
  `fs_watch:` config block was resolved (mirrors `hooks_config` defaulting to `[]`).

After the bundle returns, `self._hot_reloader` is published as the process-wide active
reloader (#2073 S3: `set_active_hot_reloader`) so the hooks-write LLM-op can
`request_reload` after writing `.reyn/hooks.yaml` (mirrors `set_active_scheduler`;
multi-session = last-registered wins, a known cron caveat). `_register_hot_reload_seams`
(#2073 S2) then registers the per-component hot-reload reapply seams once the
sub-components they orchestrate (`router_host` etc., built later by Family 6a) exist —
each seam reapplies one IN-set component live at the turn boundary, and the Session owns +
orchestrates them (a single multi-holder per-agent swap here, not scattered captures).
Hooks specifically use S2b; validate-before-apply applies too.

## Family 4 — Cost / budget

`_build_budget` constructs the budget adapter — a byte-identical extraction, the
simplest of the `#3082` families (no reordering). It runs here (unchanged position) because
it *consumes* Family 1's `chat_events`, read EAGERLY (`events=`). It returns the
`BudgetGateway` directly (`#3121` step4 removed the single-field `_CostBundle` wrapper).

Two other cost-adjacent construction points stay inline:
- `_cost_warn_config` (#2230): the resolved `cost_warn:` config so the high-cost-model
  warn/block gate actually fires in production. Without it the session had no config to
  read and the gate silently no-op'd (fail-open) — this is a production-bug fix. Always
  set (defaults when unthreaded) so the read can't `AttributeError` into a silent
  fail-open.
- `_offload_config` (tool-result-schema-redesign §5): a debug lever disabling all
  tool-result size gates (text cap / structured inline cap / media follow-up budget).
  `None` → defaults (`enabled=True`, normal offload behaviour).
- The `set_llm_call_limit_context` call publishes the per-call budget-exceed policy for
  the chat path's per-LLM-call cost gate (#1868, `call_llm`/`call_llm_tools`). It reuses
  `safety.on_limit` (one unified limit policy) and the SAME intervention path the chat-side
  limit checkpoint uses. `run_id` falls back to `agent_name` (session scope, mirroring
  `_handle_limit_checkpoint`); `non_interactive` flows through so a non-tty run fails
  closed (bounded); UNSET → fail-closed deny.
  - `#3053`: the bus is resolved BRIDGE-AWARE via `_make_router_intervention_bus` (the same
    seam `#3052` gave every MCP router-op) instead of a self-bound `_dispatch_intervention`
    captured on THIS session. Before this fix, a `safety.limit` prompt raised on an
    ATTACHED spawned/driver session (a pipeline driver, a delegated sub-agent) dispatched
    on the driver's OWN listener-less `InterventionRegistry` — silently auto-refusing
    (`enforce_listener_presence` short-circuit) without ever reaching the pipeline
    originator's live operator, violating the same intervention-delivery rule `#3052`
    fixed for MCP ops (fails SAFE here, not into a hang, since
    `handle_limit_exceeded` treats an empty/refused answer as "deny" — but still the wrong
    surface). Resolving fresh on each call (not capturing a frozen bus reference) means a
    re-bound bridge is picked up uniformly, exactly like every other router-op
    intervention.
- `_render_template_bounds` (FP-0055 / #2679): the operator `render_template` output
  bounds (`max_output_chars`/`wall_clock_seconds`), resolved once into a
  `RenderTemplateBounds` and threaded to every router `OpContext` builder. Default config
  (256,000 chars / 5.0s) is byte-identical to the prior in-handler fallback. The
  `render_template` op reads `ctx.render_template_bounds`.

## Family 5 — Retrieval

`_build_retrieval_bundle` constructs the embedding block (four attrs:
`action_embedding_index`/`embedding_provider`/`embedding_model_class`/
`embedding_event_sink`) plus `action_usage_tracker` — a byte-identical extraction (same
objects, same conditionals, same try/except None-fallbacks, same args as the inline
sequence it replaced). It stays UNMOVED, invoked at its original position, BEFORE Family 1
(`_build_audit_event_bundle`) runs — this family has no eager dependency on `chat_events`,
only the two closures' DEFERRED `self._chat_events` resolution (kept verbatim since this is
an instance method).

`_action_retrieval` (FP-0034 PR-3b-iii) drives whether the universal catalog wrappers
appear in the router `tools=`. Default constructs an off-flag `ActionRetrievalConfig` so
existing chat behaviour is preserved when callers don't pass one. `_eager_embedding_build`
(B25-S5-1 fix): when `True`, `RouterLoop` awaits the embedding index build synchronously on
the first turn (Turn 1 blocks ~2-5s) so `search_actions` is visible to the LLM from the
very first call; default `False` keeps the lazy background-build path.

## Family 6a — Router-waist (`RouterHostAdapter`)

`_build_router_waist` aggregates ~40 already-constructed Session sub-components (Families
1-5's outputs + params/early attrs set earlier in `__init__`) into `RouterHostAdapter`, the
single object most later families read through — a byte-identical extraction (same object,
same construction order, same ~40 args, including 3 DEFERRED per-turn lambdas —
`live_session_id_fn`/`current_task_id_fn`/`turn_origin_fn` — kept verbatim, still closing
over `self` and resolved at call time, not eager-ized). It stays UNMOVED, invoked at its
original position — every dependency is already set on `self` by this point.

## Family 6b — History / compaction

`_build_history_compaction_bundle` constructs `history_buffer` / `compaction_controller`
(including the None-then-patch that breaks their circular dependency) / `budget_advisor` —
a byte-identical extraction, same construction sequence, same position (right after Family
6a's `router_host`, since `history_buffer` eager-depends on it).

`_merge_action_usage_from_candidates` (FP-0019 Wave 1 / #1128 PR-a) is a nested closure
passed to the builder as a callback; the session drives compaction via
`force_compact_now()` (pre-frame guard) — background task lifecycle was removed in #1128
PR-a, all callbacks resolve against `self` at call time. `_token_learner` (PR-N6) is the
adaptive per-user token-estimation learner.

## Family 7 — Intervention

`_build_intervention_bundle` constructs `chains` / `interventions` /
`intervention_handler` / `intervention_coordinator` / `chain_timeout_glue` — a
byte-identical extraction, same construction order, same position as `chains`'s original
spot. `chain_timeout_glue` is the one exception: UP-moved from its original position
(~160 lines below, AFTER Family 8's `InterAgentMessaging`) to land here, inside this same
contiguous builder call, BEFORE `InterAgentMessaging` (which stays untouched and reads
`self._chains`) — this UP-move is safe because the F8→F7 `self._chains` cross-dependency is
preserved (F7 now runs strictly before F8, so `self._chains` is already set when F8 reads
it).

`_pending_command_ui` (F4) is a one-shot command-UI request (e.g. the `/rewind` checkpoint
picker) that a front-end renders as a selector; the inline CUI region polls it like it
polls the head intervention, and the plain `--cui` path renders a text fallback. `None` =
nothing pending; a dict carries `{"kind", ...}`.

## Family 8a — Inter-agent messaging

`_build_inter_agent_messaging` constructs `InterAgentMessaging` (FP-0019 Wave 2 part
2 — agent-to-agent messaging service, extracting `_send_to_agent`/`_send_agent_response`/
`_handle_agent_request`/`_handle_agent_response`/`_resolve_pending_chain` from `Session`;
hybrid design (案 C): `InterAgentMessaging` owns agent-side logic, transport-side routing is
handled by FP-0013 `RoutingLayer` via `send_request_callback`/`send_response_callback`
injection). Byte-identical extraction, same construction order, same (unmoved) position —
post-waist, reading Family 7's `self._chains` and Family 1's `self._chat_events` eagerly
(both already set by this point) plus a tail of deferred `self.*`/`lambda: self.*` closures
kept verbatim.

## Family 8b — Memory

`_build_memory` constructs the memory persistence adapter (PR-refactor-session-1
wave 3 PR2 — absorbs memory path resolution + remember/forget/read_body; PR3
`RouterHostAdapter` holds a direct reference, session delegates via the adapter's
`memory_path`/`memory_dir`). Byte-identical, same args as the inline construction it
replaced, unmoved — this position is PRE-WAIST, before Family 6a's `_build_router_waist`
reads `self._memory` eagerly.

## Family 8c — MCP connection service

`_build_mcp_connection_service` — see the builder's own docstring for the full deferred-
resolution crux (4 lambdas resolving `self._chat_events`/`self._router_host`/
`self._hook_dispatcher`/`self._interventions` at CALL time, none of which exist yet at this
position in `__init__`).

## Capability, permission & visibility

- `_exclude_tools` (#187) — tool names excluded from the MAIN chat `RouterLoop`'s
  LLM-visible catalog, threaded to the loop construction below. General capability (mirrors
  the sub-loop `exclude_tools`, `planner.py:1136`); the faithful SWE-eval excludes
  `web__search`/`web__fetch` so the agent solves from the repo + issue, not a web lookup of
  the gold solution.
- `_contextual_permission` (#1827 S3) — per-session `capability_profile` narrowing
  (`ContextualPermission`) resolved from the agent's topology role. Threaded to the live
  tool gate (`RouterLoop`) + control-IR `OpContext`. `None` = no narrowing (byte-identical).
  `_untrusted_contextual_cache` (#1827 S4b, context-auto) is a lazily-resolved minimal
  `_untrusted` profile `ContextualPermission`, composed into the per-turn narrowing while
  untrusted external content is live in context; `None` until first needed.
- `_excluded_categories` (#1667) — catalog categories hidden at the universal-catalog
  source (e.g. `reyn_repo` on the external-repo eval path so it doesn't compete with
  `file__*` for the weak model); interactive default empty = `reyn_repo` kept.
- `_visibility_override` (#2285) — session-scoped LLM tool-VISIBILITY override: the
  capabilities the user toggled OFF via the status bar, per kind. Applied as one more
  restrict-only ∩ conjunct ON TOP of the re-resolved agent envelope
  (`_reapply_visibility_override`), so it can only HIDE within the authorized set (visible
  ⊆ authorized by construction). In-memory (step1 live); step2 was planned to persist it to
  the per-session `config.yaml` so `resolved_profile_for(sid)` re-derives it.
- `_disabled_hooks` (#2285) — session-scoped hook APPLICABILITY override: hook names the
  user disabled via the status bar. The `HookDispatcher` (per-session) skips a hook whose
  name is in this set at dispatch time (live). Per-session by construction: each `Session`
  owns its own dispatcher + this set, so disabling a hook in session S1 does NOT affect S2
  (even though the hook config is shared). In-memory (step1 live); step2 was planned to
  persist to the per-session `hooks.yaml`.
- `_task_subscription_writer` (#2187 backend-master) — the Task SUBSCRIPTION writer (the
  Reyn-internal task↔session binding WRITE seam), threaded down the same chain as
  `task_waker`.
- `_spawned_tasks` (#2103 S1bc-exec) — sid → original-task record for sessions THIS session
  spawned. When a spawned session's result routes back, the result header renders
  `task=<the spawner's OWN request>` from THIS trusted record (keyed by the spawned sid) —
  never the spawned session's echo (which a compromised sub-session could forge into
  trusted framing). Bounded-by-construction: evicted on result arrival; a max-size cap
  (evict-oldest) caps a never-arriving result.

## Multimodal / media

- `_multimodal_config` (#364) — media-size gate config plumbed through to spawned Agents
  AND to the router host adapter (chat-router `web__fetch`/`file__read`/mcp paths).
- `_media_store` (#383 PR-C) — a single `MediaStore` instance per Session, constructed from
  the multimodal config's storage dirs, then threaded into spawned Agents (for control-IR
  ops invoked from sub-agents) AND into the router host adapter (for ops invoked directly
  from the chat router via tool calls). `None` when no multimodal config is supplied —
  handlers fall back to the pre-#383 inline shape. `agent_name` (β core impl sub-task 1) is
  set so path-refs minted by this session carry `resource_uri`/`source_agent` so cross-host
  consumers (other agents via A2A/MCP/Browser) can dispatch back here. `base_url` (β core
  impl sub-task 3b) is set only when this Reyn instance is reachable over HTTP (operator
  sets `multimodal.base_url` in `reyn.yaml`), so path-refs also carry a `url` field
  pointing at the resources router — cross-host consumers can then HTTP GET the body; when
  unset, only same-host `path` is available.
- `_pending_user_images` (#366) — queue of image blocks the user attached via `/image PATH`
  or `--image PATH`, drained on the next user-message turn (attached to that
  `ChatMessage`'s `media` field). litellm-style content parts:
  `{"type": "image_url", "image_url": {"url": "data:...;base64,..."}}`.

## Safety, limits & interactive mode

- `_router_max_iterations` (#187) — per-message tool-call budget for the MAIN chat
  `RouterLoop`. The interactive default (5) suits a human turn; an autonomous one-shot run
  (`reyn chat --once` for SWE) needs far more (explore→edit→verify rounds), so the one-shot
  path constructs the session with a higher value. Bounded either way — the loop stops at
  the cap (finite) or when the agent ends.
- `_non_interactive` (#1439 Fix #1) — in run-once (no interactive user) the router SP must
  not tell the agent to "ask ONE clarifying question" (nobody answers → dead stop,
  `#13398`). Threaded to `build_system_prompt`. Default `False` = interactive
  byte-identical.
- `_hook_driven_turns` (#1800 slice 7) — the loop-valve counter: hook-driven (`kind="hook"`)
  turns since the last human user turn. In-memory only (NOT snapshot-persisted); resets on
  each user turn (re-arm). Bounds hook self-continuation.
- `_next_turn_context` (#1800 slice 4b) — in-memory staging buffer for `wake=false`
  ride-along (C) messages drained by `_drain_to_wake`. Entries are applied to the next
  trigger's turn as attributed system-role history entries. Persisted durably in the
  snapshot (decision B) via `_journal`; restored by `restore_state`. Cleared (durably)
  after injection at the trigger turn.
- `_buffered_intervention_answers` (PR-intervention-link L6) — in-memory buffer of answers
  from restored-then-resolved interventions, keyed by `run_id`. The first `bus.request`
  from the resuming `run_id` consumes the entry and returns it without re-dispatching.
  Persistence across the (user_answered → process_crashed → run_not_yet_resumed) window is
  R-D12 follow-up.
- `_current_task_id` (#1953 §16, recursive-request) — the `task_id` this session is
  currently EXECUTING as a task-as-request, set per-turn from an execute-wake's meta
  (`run_one_iteration`). Read by the router op-ctx builders so `task.create` derives
  ownership (`requester=<this task>`). `None` = not executing an assigned task (a user /
  hook / recovery turn). Slice B extends the lifetime to a persistent assignment spanning
  continuation + recovery turns.
- `_current_turn_origin` (proposal 0060 Phase 1 Layer A, A7) — the OS-authoritative
  provenance classification of the turn currently being processed, mirroring
  `_current_task_id` exactly (same seam, same threading). Set per-turn in
  `_stamp_execution_context`; read by the router op-ctx builders so install-op handlers
  (skill/pipeline/present, A9) stamp `entry["provenance"]` from a single OS-set source the
  LLM cannot spoof. Initialized to the STRICTER value (fail-safe: never default to
  `"user_directed"` before the first turn is classified).
- `_ephemeral` / `_vanish_scheduled` / `_vanish_task` (#2103) — a spawned EPHEMERAL session
  (spawn-time `mode="ephemeral"`) auto-vanishes once its task is done. Set
  post-construction by the registry on an ephemeral spawn; the main session + persistent
  spawns leave `_ephemeral` `False`. `_vanish_scheduled` guards against a double-schedule
  across turns.

## Misc lifecycle wiring

- `_on_perm_persist_cb` (#398 v4 emitter wiring, permission_manager → state_change) —
  subscribes to `_persist` events on the shared `PermissionResolver` so a permission
  grant/revoke mints a `state_change` history entry in this session; the LLM sees
  "permission for X was granted" in its next turn and breaks out of the #352 refusal trap.
  Stored as a bound method so the same reference can be unregistered on session shutdown.
- `ChatLifecycleForwarder` (#162) — surfaces session-level lifecycle events (compaction
  today; attach/detach + budget warnings as growth) into the conv pane via
  `OutboxMessage(kind="system")`. Given THIS session's own `EventLog` (#2708 P3.1 Half-B)
  so its driver→parent bridge (`on_pipeline_run_attached`) can re-emit a driver `presented`
  audit event onto the PARENT's log (`bridged_from=<driver_sid>`), closing the split audit
  trail the visible-output bridge (Half-A) leaves.
- `_on_chat_event_for_state_change` (#398 v4 emitter family) — a generic events-log
  subscriber converting known op-emitted events (`mcp_server_installed`, future:
  `config_reloaded`/`sp_version_changed`) to `state_change` history entries via the
  `_STATE_CHANGE_EVENT_MAPPINGS` dispatch table. Sister to the `permission_manager`
  direct-callback wiring (PR #456).
- `_presentation_consumer` (#2708 P1) — the present-sink consumer. In production it is
  always supplied by `build_scoped_chat_session` (required kwarg); a direct/test
  construction (`None`) falls back to the outbox-backed consumer so the per-turn
  `OpContext` still wires an `OutboxPresentationRenderer` (byte-identical to the removed
  uniform default). The renderer is obtained lazily (`sink(self)`) so it can bind this
  Session — no `OutboxPresentationRenderer` is instantiated at this call site; the AST
  guard (`test_present_sink_ast_guard_2708`) requires the sole construction site to be
  `OutboxPresentationConsumer.sink()`.
- `_intervention_bridge` (#2708 P3.2a) — the spawn-time intervention bridge (`None` =
  self-bound default). When set (attached pipeline driver), the router
  `intervention_bus_factory` builds a bus bound to the PARENT session so the driver's
  `ask_user` reaches the parent's live operator listener by construction (mirror of
  `_presentation_consumer`).
- `_pipeline_registry` (IS-5 / #2575) — Session owns a live `PipelineRegistry` so
  `run_pipeline` has a registry to look up against. The session factory builds it ONCE from
  `config.pipelines` (disk scan → parse → register) and passes it in; a direct/test
  construction with no registry falls back to an empty one (byte-identical to the
  pre-#2575 own-constructed empty registry). Threaded to `RouterHostAdapter` (mirrors
  `agent_registry=self._registry`) → `RouterCallerState.pipeline_registry` → the universal
  catalog's `pipeline` category enumerator surfaces each registered pipeline as
  `pipeline__<name>` to the LLM (IS-5 D19).
- `_presentation_registry` (FP-0054 PR-C) — the session's named-presentation-template
  registry. Threaded to `RouterHostAdapter` (mirrors `pipeline_registry`) → each router
  `OpContext`'s `presentation_registry` → the `present` op's stage-1 template resolution.
  The hot-reload seam (`_reapply_presentations`) SWAPS this reference AND the adapter's
  captured copy so a newly-registered template is visible at the next turn boundary. `None`
  (direct/test) → empty registry.
- `_max_hop_depth` (PR11) — max delegation hop depth (LangGraph-style). `0` = user input,
  each `_send_to_agent` increments; refuse send when depth > limit.
- `_chain_timeout_seconds` (PR18) — per-chain wall-clock budget. Non-positive disables.
  When the budget elapses, the runtime synthesizes an error response upstream so a chain
  stuck on a non-responsive delegate doesn't hang forever.
- `_on_limit` / `_safety_extensions` (FP-0005) — per-session safety-limit checkpoint
  policy, and per-(turn or chain) extension counters granted by
  `_handle_limit_checkpoint`, cleared on turn/chain boundary by the relevant call sites.
- `_allowed_mcp` (PR37) — optional MCP server allowlist from agent profile. `None` = no
  per-agent restriction (inherits project config); `list[str]` = only these servers pass
  the per-agent check in `require_mcp`.
- `router_config`/`retry_config` (#1829 S3b / #1835) — published as ambient ContextVars
  for the LLM chokepoint (`set_router_config`/`set_retry_config`), guarded so they're only
  set when provided — a nested construction never clobbers an inherited ContextVar with
  `None`. Runs spawned within this session inherit the ContextVar (propagation).
- `_loop_driver` (session.py refactor PR-3) — `RouterLoopDriver` owns the per-turn loop
  orchestration (run_turn, shrink/overflow, cap enforcement, cancel); an injectable seam
  (`loop_driver` param) replaces the default construction, e.g. for tests.
- `_cancel_forward_targets` (#2588) — additional cancel-forward targets.
  `cancel_inflight` always cancels this session's OWN `_loop_driver` (the turn); it ALSO
  fires `request_cancel` on every callable registered here. Populated only transiently —
  e.g. `run_pipeline_attached` registers the spawned pipeline driver-session's
  `request_cancel` for the duration of a sync attached run, so a Ctrl-C on THIS (the
  attached caller) session reaches the driver-session's cooperative cancel flag (the
  executor's step-boundary `cancel_check`). Empty for every ordinary turn, so the normal
  turn-cancel path is byte-identical when nothing is registered.
