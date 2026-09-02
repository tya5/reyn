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

`agent: Agent` is a **required** `__init__` param — the sole source of identity
(#3133 Priority-0 step-2). There is no `agent=None` fallback and no duplicate flat
identity params (`agent_name`/`model`/`permission_resolver`/`workspace_base_dir`/
`workspace_state_dir`/`sandbox_config`/`sandbox_backend`/`environment_backend`/
`agent_role`) on `Session.__init__` — they were removed together with the
`Agent(...)` fallback construction they fed, so `agent_name != agent.agent_name`
is no longer constructible. In production, `build_scoped_chat_session` is the
single chokepoint that assembles the real `Agent` and passes it in as `agent=`.
Tests build an `Agent` explicitly too — `tests/_support/agent_session.py`'s
`make_session` helper (and the compaction helper in `tests/_support/session.py`)
do this for every test call site, so no test constructs `Session` with the old
flat identity kwargs either.

`agent_name`/`model`/`_perm`/`_workspace_state_dir`/`environment_backend`/
`sandbox_backend`/`workspace_dir`/`agent_role` are then read-only `@property` delegations
to `self._agent` (defined later in the class body) — external code reads them exactly as
before; only the storage moved. `_workspace_base_dir` (#4200/#5081/#5084) and
`_sandbox_config` (#5352) are the two exceptions: each COMPOSES `self._agent`'s own
value with session-layer + agent-layer overrides (see their own field notes below)
rather than delegating to it bare.

Field-by-field notes on the identity cluster (now Agent-held, exposed via the `@property`
block):
- `_sandbox_config` — exec-tool backend policy, plumbed to spawned Agents. #5352:
  now a COMPOSED property, not a bare delegation — `self._agent.sandbox_config`
  (the process-wide default, byte-identical for every agent per that field's own
  disclosure) with `.policy` REPLACED wholesale when either layer below has one:
  a sid-keyed session-layer override (`apply_per_session_sandbox`, the #2126
  shape — resolved by the registry at spawn time per the same-agent /
  cross-agent-declared / cross-agent-undeclared priority table and re-injected
  before the child's first turn) wins over this agent's own `profile.yaml`
  `sandbox:` declaration (a live re-read, same shape `_workspace_base_dir` uses
  for `base_dir`) wins over `None` (no override at either layer).
- `_environment_backend` (#1200 PR-F1) — the agent's `EnvironmentBackend` INSTANCE for the
  chat FS seam (the router `Workspace` built in `make_router_op_context`); `None` → the
  workspace's own `HostBackend` default.
- `_workspace_base_dir` / `_workspace_state_dir` (#187) — the chat `OpContext`
  `Workspace`'s FS root + host-side state dir. With a container env-backend the repo lives
  *inside* the container, so `base_dir` must be the container repo root (the partner of
  `build_environment_backend`'s backend) — otherwise `read_file`/`grep`/`glob` resolve
  against the host cwd and the agent never sees the target tree (the #187 step-3 empty-FS
  defect this param closes).
- `output_language` (#4206 slice 1) — public `@property`, NOT a plain constructor-set
  attribute since this slice: live-resolves `reyn.runtime.preferences`'s ③ (free-override)
  axis — session-layer `<session-state-dir>/config.yaml` `preferences.output_language` wins
  over agent-layer `profile.yaml` `preferences.output_language` wins over
  `_project_output_language` (the value this `Session` was constructed with, `config.
  output_language` for the default factory). Live re-read on every access, the SAME
  "session layer in front of agent layer, re-read every time" shape `_workspace_base_dir`
  below already established for `base_dir` — an operator editing either file by hand takes
  effect on the next read, not just the next process start.
- `reasoning_display` (#4206 slice 2) — public `@property`, the SAME shape as
  `output_language` immediately above, factored through a shared
  `Session._resolve_session_preference(key, project_default)` helper both now
  call. Resolves ONLY `chat.reasoning.display` — `chat.reasoning.continuity`/
  `recent_turns` stay ② bounding (read directly off `self._reasoning`,
  unaffected). `RouterHostAdapter.reasoning_display_enabled()` consults this
  live via a `reasoning_display_fn` callback wired at construction (the SAME
  callback shape `reasoning_continuity_section_fn` already established for
  the sibling continuity-section renderer) — `None` (every pre-slice-2 host)
  falls back to the frozen `reasoning_config.display` read, byte-identical.
- `warn_ratio_overrides()` (#4206 Slice B, #4724) — public METHOD (not a
  property — returns a fresh `dict[str, float]` each call, not a scalar), the
  ③ resolution for the 7 `cost.*.warn_ratio` keys. A DIFFERENT shape from
  `output_language`/`reasoning_display` above: `BudgetTracker` is
  process-shared (one instance across every agent/session in a process), so
  it cannot resolve a session/agent identity itself the way a per-`Session`
  property can — this method collects only the keys actually overridden at
  either the agent or session layer (an omitted key means "use the
  tracker's own project-level ratio, unchanged") and the CALLER
  (`RouterLoop`/`BudgetGateway`, via `RouterHostAdapter.
  warn_ratio_overrides()` / `BudgetGateway`'s own `warn_ratio_overrides_fn`
  callback, both wired at construction the SAME way `reasoning_display_fn`
  is) passes the resulting mapping into `BudgetTracker.check_pre_llm`/
  `record_llm`/`format_budget_full` as an explicit argument ("Design C",
  #4724 — no process-shared override registry, no session-id plumbed into
  the tracker itself).
- `model_class_ceiling` (#4206 ②) — public `@property`, the ②bounding
  axis's ONE key so far (`model`). A DIFFERENT composition from
  `output_language`/`reasoning_display`/`warn_ratio_overrides` above: those
  are ③ (free-override, last-present-wins); this one is restrict-only
  (narrowest-of-project/agent/session wins, via
  `reyn.runtime.bounding.compose_model_ceiling`) because `model` consumes a
  shared, bounded resource (`BudgetTracker`'s quota) that a free override
  could exhaust. Composed from `self._resolver.class_ceiling()` (project)
  + this agent's `profile.yaml` `bounding.model` + this session's own
  `config.yaml` `bounding.model` (session wins over agent when both narrow,
  same precedence order ③ uses) — a layer that declares a WIDER class than
  a layer above it never widens the effective value. Live re-read on every
  access, same shape as the ③ properties above. `RouterHostAdapter.
  model_class_ceiling()` consults this live via a `model_class_ceiling_fn`
  callback wired at construction (the SAME callback shape
  `reasoning_display_fn` established) — `None` (every pre-② host) falls
  back to `resolver.class_ceiling()` directly, byte-identical to
  `RouterLoop`'s prior construction-time-cached read. The composed value
  feeds the SAME #1190 chokepoint (`recorded_acompletion`) `model_class_ceiling`
  has always fed — ② only changes WHERE that value comes from, not the
  enforcement itself.
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

`_build_audit_event_bundle` constructs `event_store` → `audit_events` (`EventLog`) →
`outbox_hub`, plus the opt-in OTEL subscriber attached to `audit_events` when an OTLP
endpoint is configured (P5 ADR-0039: config value or `OTEL_EXPORTER_OTLP_ENDPOINT` env).
`None`/no-endpoint → not attached, zero overhead, byte-identical to no-OTEL. This is
Family 1 because several later families (3, 4, 6a, 8a) eagerly read `self._audit_events`
at construction and must run *after* it — the ordering constraint that pins Family 1 to
run before Families 3/4/6a/8a below.

`self._audit_events` is also published as the process's ambient `EventLog` sink for the LLM
`acompletion` chokepoint (#1669: `set_llm_request_event_log`) — every in-session LLM call
emits an observable `llm_request` event without threading events through the call stack.

## Family 2 — Recovery (WAL / journal)

`reyn.runtime.services.recovery.build_recovery` constructs `generation_store` → `journal`
(`SnapshotJournal`, extracted in PR-refactor-session-1 wave 2 — the session keeps
`_snapshot_path` only for diagnostic logging; the journal owns the actual I/O). Session no
longer builds this pair itself: every construction site (the scoped session factory, the
test helpers) calls `build_recovery` and passes `generation_store=` / `journal=` in as
required `Session.__init__` params (recovery-bundle-out-of-Session refactor). The default
`.reyn/agents/<agent_name>/state/snapshot.json` path convention lives in
`reyn.runtime.services.recovery.default_snapshot_path`, the single source both `Session`
and its callers derive from. `build_recovery` reads the *caller's local* `state_log` value,
not `self._state_log` off an already-constructed Session. `state_log` is process-shared
(owned by `AgentRegistry`); `None` disables persistence (tests / non-chat invocation). `_session_id`
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
- `_background_tasks` (`TrackedTaskSet`, #4759) — the single funnel every background
  task this session (or a sub-component it owns) spawns via `asyncio.create_task`
  routes through, replacing a prior per-field enumeration (`_inflight_wal_tasks` was
  one such field, ADR-0038 Stage 1c coverage; folded in by #4759). Each registration
  carries two INDEPENDENT axes (`tracked_tasks.py`'s own module docstring has the
  full rationale, including a first-attempt axis name that shipped a real
  regression, #4759/#4765 co-vet): `disposition` (`"cancel_join"` vs `"await"` —
  HOW a task folds) and `appends_wal` (WHETHER it's safe to fold it DURING a
  rewind, not just at shutdown). Fire-and-forget WAL-append tasks (intervention
  dispatch / `intervention_answer_consumed`) register with `disposition=
  "cancel_join"`, `appends_wal=True` via `_track_wal_task`; `await_quiescent`
  calls `aclose(appends_wal=True)` — the WAL-appending subset ONLY, not the whole
  set — so no such append can land past the rewind reset-record seq, without also
  tearing down `appends_wal=False` mechanisms (OutboxHub's drain loop, the
  hook-bus bridge, …) that the session still needs mid-rewind. The unfiltered
  drain (`aclose()` with no `appends_wal` filter) lives at
  `Session.aclose_background_tasks`, reached only from real shutdown
  (`AgentRegistry.shutdown()`). See `tracked_tasks.py`'s own module docstring for
  the root cause this replaced and why a single owned
  collection, not another named field, is the fix.
- `_state_log` is also kept directly (not only via the journal) because ops launched from
  this session need it to emit step events into the same WAL that the journal writes to.
- `_halted_reason` (#2259 PR-3) — set when the session FAIL-STOPS (e.g.
  `"durability_failure"`); `None` while running. In-memory only (durability is dead → it
  cannot itself be a durable event) — the operator-visible pair to the raised
  `DurabilityHaltError`. #2280: the first time this latches (on EITHER the accept-edge
  `_put_inbox` raise or the process-edge `run_one_iteration` halt — guarded so it fires
  once), a `session_halted` audit-event carrying `reason` is emitted, so an operator who is
  IDLE (not currently submitting an op) learns the halt proactively — the TUI status line
  (`interfaces/inline/textual_chat/chrome.py`'s `status_line_text`) and the plain `--cui`
  renderers' `bottom_toolbar` both surface it via this event, never by polling
  `halted_reason` on a timer. Purely observability; the raise/halt above remain the whole
  safety mechanism.
- **How `DurabilityWorker` reaches the §4-exhausted state above** — a durable-write
  failure is classified by exception type: `OSError` (disk-full, EIO, a transient
  filesystem hiccup) is retried with bounded backoff (`max_write_attempts`); a
  non-`OSError` (a real bug — retrying cannot help) raises immediately, no retry.
  Exhausting the bounded retries is what escalates: raised to the submitter for a
  blocking `submit`, or latched to `durability_failed` (→ `_halted_reason` above)
  for a fire-and-forget write. One task's escalation does not kill the worker —
  it keeps serving subsequent submissions, so a single bad write doesn't wedge
  every other session's durability.
- `_queue_seq` (`#3300` P2a) — monotonic sent-queue-mutation counter, an order-race gate for a client merging the granular `user_submitted`/`turn_started` queue deltas (see `_bump_queue_seq` for the full rationale). Deliberately NOT WAL-durable: it is a client read-model liveness aid (resolves snapshot/delta interleaving on the wire), not recovery state. A restart safely resumes from 0 because a fresh connection's `STATE_SNAPSHOT` always seeds the client's last-applied seq before any delta is merged — there is no window where a delta referencing a pre-restart seq reaches a client that hasn't first received the fresh snapshot's baseline.
- `cancelled_msg_ids` (`#3300` P3, Y-server; moved off `Session` onto `InboxArbiter` in `#3978` P1, née `_cancelled_msg_ids`) — an in-memory skip-at-consume set for `msg_id`s cancelled via `cancel_queued` while still sitting in the (durable) `asyncio.Queue`, whose entry cannot be removed in place (no such API exists). `InboxArbiter.consume_inbox`/`drain_to_wake` discard a dequeued item whose `msg_id` is in this set instead of dispatching it. The item's `snapshot.inbox` entry and the WAL `inbox_cancel` tombstone are recorded SYNCHRONOUSLY at cancel time, independent of this deferred physical dequeue (see `cancel_queued` / `SnapshotJournal.cancel_inbox`). This is what makes the in-memory-only set safe: a crash before the dequeue leaves no stale Queue entry to skip, because a fresh process starts with an empty `asyncio.Queue` and repopulates it from the recovered snapshot — which already excludes the cancelled item (see `restore_state`).
- `pending_inbox_items` (`#3792`, widened by `#5647`; moved off `Session` onto `InboxArbiter` in `#3978` P1, née `_pending_inbox_item`) — a volatile (not WAL/snapshot-backed) peek buffer for mid-turn injection, holding items in ARRIVAL ORDER. `InboxArbiter.peek_mid_turn_injection` dequeues via `inbox.get_nowait()` into this buffer WITHOUT telling the journal (`asyncio.Queue` has no peek/un-get API), so the snapshot-backed SSoT stays untouched until `Session._commit_mid_turn_injection` (stayed on `Session` — the extraction's scope was inbox-drain/dispatch-attribution state, not this commit path) actually commits it. `consume_inbox` drains this buffer from its HEAD first, ahead of a fresh `inbox.get()` — this is what makes an item that was peeked but never committed surface exactly where it would have if the peek had never happened, rather than being stranded or reordered. `peek_mid_turn_injection` returns `list[dict]` (`#5677`, was a single `dict | None`) — every currently-eligible item found in one non-blocking scan, not just the first, batched into one wire splice.

  It was a single slot until `#5647`. `#3792` STOPPED the peek at an ineligible-origin head rather than looking further, on the reasoning that skipping would silently reorder arrival and leave no trace. Measured against a real deployment that inverted: on reyn-self a `broker_drain` hook posts inbox items continuously, so an operator's prompt was almost always queued behind one and mid-turn injection — the feature that exists so a human can steer a running tool loop — never fired. `#5647` (architect ruling A) lets the peek look PAST ineligible items to the first eligible one, holding what it looked past in this buffer. Both halves of the original objection are answered rather than dropped: **arrival order is not reordered** (the buffer is FIFO and is drained before the queue, so turns still start in arrival order — only which message the model sees first WITHIN a turn changes), and **the trace exists** (the mid-turn `turn_started` event carries `skipped_over`, enumerating the kind and msg_id of everything looked past, in arrival order; empty list when nothing was). Eligibility is `TurnOrigin.MID_TURN_INJECTABLE` (`turn_origin.py`) — `CLIENT_INPUT` (`#3595` provenance gate) and, since `#5677`, `AGENT_REQUEST` too. Each kind renders on the wire/history under its own `TurnOrigin` (`Session._render_mid_turn_injection`, `#5677` §0) — `CLIENT_INPUT` stays `role="user"`; every other eligible kind renders `role="system"`, attributed, never hardcoded to `role="user"`.
- `_turn_cancel_self_initiated` (`#2242`) — `True` only for the window between `cancel_inflight()` calling `_turn_owner_task.cancel()` and `run_one_iteration` observing the resulting `CancelledError`. It distinguishes OUR OWN hard-cancel (which the run-loop swallows so the driver task survives) from an externally-cancelled driver task — e.g. an anyio scope teardown cancelling the MCP/A2A request-handler task that pumps `run_one_iteration` directly (FP-0013 §ADR-A). In the external case, `await self._turn_owner_task` ALSO raises `CancelledError` (asyncio propagates an awaiting task's cancel into whatever Task/Future it is suspended on) — but that cancellation must be RE-RAISED, not swallowed, so the driver's own cancellation completes normally instead of silently surviving a cancel that was never ours. Confusing the two makes the runtime's cancellation bookkeeping and the caller's anyio scope diverge: the scope believes its teardown cancel completed; the driver task is still alive.
- **RE-DRAIN LOOP** (`#2115`, generalised by `#4759`) — `await_quiescent` calls
  `self._background_tasks.aclose(appends_wal=True)` (`TrackedTaskSet`,
  `tracked_tasks.py`) to cancel every `"cancel_join"`-disposition,
  `appends_wal=True` tracked task (cancel, not join-only: the intervention-dispatch
  task awaits the user-answer future indefinitely, and the tasks are drop-safe so
  cancelling is correct) and join every `appends_wal=True` task currently tracked,
  looping to a fixpoint (bounded by `tracked_tasks.py`'s own `_MAX_ACLOSE_ROUNDS`,
  née `await_quiescent`'s own `_QUIESCE_MAX_ROUNDS` before #4759 moved the loop
  into the shared primitive) rather than a one-shot pass. A joined task can
  register a NEW tracked task DURING the `gather` — a one-shot snapshot-then-drain
  misses that reschedule (this was `#2115`, originally scoped to WAL-append tasks
  alone: a straggler append landing after `await_quiescent` had already returned,
  i.e. after the global-rewind reset-record was appended). Looping until the
  `appends_wal=True` subset is fully drained closes that window, now for every
  WAL-appending producer registered through the funnel, not just the original
  two. Deliberately NOT unfiltered: `await_quiescent` runs during a rewind, not
  only at shutdown, and an unfiltered `aclose()` here regressed once already (CI
  caught a session that silently stopped answering after a rewind, because the
  unfiltered drain also killed `appends_wal=False` mechanisms like OutboxHub's
  drain loop that the session still needs — #4759/#4765 co-vet; see
  `tracked_tasks.py`'s own module docstring for the full regression story and why
  the axis is named by WAL-appendability, not by task lifetime). On reconstruct,
  `restore()` re-arms timers from the recovered snapshot, so cancelling here
  remains reversible. See `TrackedTaskSet.aclose`'s own docstring for the
  re-entrancy case (a tracked task itself calling `aclose()`, e.g. the
  ephemeral-vanish task's own `await_quiescent` call) and exactly which
  guarantee that case weakens.
- **Wall-clock bound on the whole call, rewind-path only** (`#4771`) — the
  RE-DRAIN LOOP above is bounded by round count, not by time; `await_quiescent()`
  itself stays unbounded by design (its own docstring's "critical invariant":
  returning early risks a straggler append landing past the reset-record).
  `AgentRegistry.checkout`'s step 3 wraps each session's call in
  `_await_quiescent_bounded`, an `asyncio.wait_for` with a per-session bound
  (`_quiesce_bound_s` — one `_MCP_CLIENT_CLOSE_WORST_CASE_S` unit, 6.5s, per
  currently-held MCP connection, floored at one unit for a connection-less
  session). On timeout it raises `RewindQuiesceTimeoutError` and `checkout`
  aborts BEFORE the reset-record append (step 4) — fail-safe, not fail-open:
  unlike `shutdown()` (safe to abandon a straggler because the process exits
  right after), a rewound session keeps running, so proceeding past an
  unquiesced session risks the exact silent-corruption class this whole
  mechanism exists to prevent. Scoped to the rewind/`checkout` call site only
  — `await_quiescent()` itself is unchanged for every other caller.

## Family 3 — Hook-event / reactivity

`_build_hook_event_bundle` constructs `hook_bus` → the awaited `hook_dispatcher` →
`fs_watcher` → `composer_registry` → `composed_consumer` → `hot_reloader` together. It runs
right after Family 1's `audit_events` assignment because this family *consumes*
`audit_events`: `hot_reloader` reads it EAGERLY at construction (`events=audit_events`), and
`hook_bus`/`hook_dispatcher`/the Composers emit through deferred `self._audit_events`
lambdas.

The config-derivation this builder takes as inputs is resolved inline, BEFORE the builder
call:
- Hooks are LAYERED (#2073 S2b, #5505): the `reyn.yaml` startup layer (OUT-set, captured
  once as `_startup_hooks_raw`, never re-read on reload) ∪ the `.reyn/config/hooks.yaml`
  runtime layer (IN-set, hot-reloadable; the LLM-op writes it in S3) ∪ the trusted
  per-agent layer (#5505: `.reyn/config/agents/<name>/hooks.yaml`, captured once as
  `_trusted_per_agent_hooks_raw` — boot-only like startup, fail-loud like startup, NOT in
  the IN-set) ∪ per-agent ∪ per-session. `_build_hook_registry` combines all five, in
  `HOOK_ORIGIN_ORDER`'s own order; the boot registry includes the runtime layer too (active
  from session start, mirroring `.reyn/mcp.yaml`), and the hooks-reapply seam re-reads only
  the runtime/per-agent/per-session layers + re-combines (trusted-per-agent, like startup,
  never re-reads).
- Composers mirror the PRE-#5505 hooks layering (Hook-Event Redesign Phase 4b/5,
  #2880/#2881) — `hooks:`-only, #5505's trusted-per-agent layer was out of that issue's
  scope: `_startup_composers_raw` captures the `composers:` startup (OUT-set) layer once;
  `_build_composer_defs` combines it with the runtime/per-agent/per-session layers (its own
  4-layer additive shape — `_build_hook_registry` gained a 5th layer, composers did not).
  Composers are v1-startup-only — no hot-reload/reapply seam, unlike hooks (restarting a
  live Composer's `PendingStore` mid-session is a separate, not-yet-designed concern).
- `_build_composer_pending_store` (#3180) runs inside the Family 3 builder and returns the
  `DurablePendingStore` shared by every `durable` composer (`op: deadline` by default), or
  `None` when no definition asks for durability — so a durability-free session writes no
  file. It reads/writes `<per-session state dir>/composer_pending.json` (the same dir
  `_toggle_store_dir` uses), and prunes restored records whose composer no longer exists in
  the combined config, so a renamed deadline cannot leave an arm nothing will ever disarm.
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
`request_reload` after writing `.reyn/config/hooks.yaml` (mirrors `set_active_scheduler`;
multi-session = last-registered wins, a known cron caveat). `_register_hot_reload_seams`
(#2073 S2) then registers the per-component hot-reload reapply seams once the
sub-components they orchestrate (`router_host` etc., built later by Family 6a) exist —
each seam reapplies one IN-set component live at the turn boundary, and the Session owns +
orchestrates them (a single multi-holder per-agent swap here, not scattered captures).
Hooks specifically use S2b; validate-before-apply applies too.

### FsWatcher hook_trigger — deferred dispatcher lambda (#2608 H4)

`fs_watcher` (the session-owned filesystem watcher; see `reyn.runtime.fs_watcher`'s
module docstring for the thread→async bridge design) is constructed
unconditionally inside `_build_hook_event_bundle` — this is cheap: no OS
thread is spun up at construction, only inside `FsWatcher.start()` (called
later, from `run()`).

`hook_trigger` is the same deferred-lambda-over-`self._hook_dispatcher`
pattern this family's H1 wiring uses elsewhere in this builder: at the point
`fs_watcher` is constructed, `hook_dispatcher` is only a LOCAL variable in
this builder — it is unpacked onto `self._hook_dispatcher` by the caller
only after this builder returns. The lambda is never CALLED until
`FsWatcher.start()` is awaited from `run()`, long after `__init__` has
finished, so the deferred read is always safe.

Binding `self._hook_dispatcher` eagerly at this call site instead (e.g.
`hook_trigger=self._hook_dispatcher.dispatch`) raises `AttributeError`
immediately at Session construction, since the attribute does not exist on
`self` yet — this fails loud (any test constructing a Session hits it).

`paths` / `debounce_seconds` default to empty / `0.2` when no `fs_watch:`
config block was resolved — this mirrors `hooks_config` defaulting to `[]`.

### Composer registry / consumer construction vs start (#2880/#2881)

`composer_registry` and `composed_consumer` (the Hook-Event Redesign Phase
4b/5 Composer definitions + the composed:*→Sync consumer bridge) are built
inside `_build_hook_event_bundle`, but neither is STARTED there. Starting a
component means spawning background asyncio tasks — that requires `run()`'s
async execution context, not `__init__`'s synchronous one.

`run()` calls `self._composer_registry.start()` / `self._composed_consumer.start()`
once, near its top; both are `stop()`ed in `run()`'s shutdown `finally` block —
the same start/stop shape `FsWatcher` uses (constructed unconditionally,
started/stopped only from `run()`).

Calling `.start()` from inside `__init__` instead would try to schedule
background tasks with no guaranteed running event loop bound to this
session's lifecycle, and would race ahead of sibling components (e.g.
`router_host`, built later by Family 6a) the composed-consumer bridge may
need live.

## Family 4 — Cost / budget

`_build_budget` constructs the budget adapter — a byte-identical extraction, the
simplest of the `#3082` families (no reordering). It runs here (unchanged position) because
it *consumes* Family 1's `audit_events`, read EAGERLY (`events=`). It returns the
`BudgetGateway` directly (`#3121` step4 removed the prior single-field wrapper dataclass).

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

`_build_retrieval_bundle` constructs the embedding block (three attrs:
`action_embedding_index`/`embedding_provider`/`embedding_model_class`) — a
byte-identical extraction (same objects, same conditionals, same
try/except None-fallbacks, same args as the inline sequence it replaced).
The call site sits right after Family 1 (`_build_audit_event_bundle`)
rather than before it.

**#4552 (2026-08) — `action_usage_tracker` removed.** This family used to
also construct `action_usage_tracker` (the hot-list feature's freq+recency
ranking, `ActionUsageTracker`), plus an `_on_hot_list_changed` closure that
emitted a `hot_list_updated` audit-event on every ranking reorder — removed
with the hot-list feature (owner directive: discarded, superseded by
`list_actions` as the canonical discovery path). `_build_retrieval_bundle`
dropped its `agent_name`/`audit_events` params along with it (both existed
solely to feed the tracker's persist path and the closure's audit-emit,
respectively — nothing else in the builder read them).

The reordering this family's call site still carries (#3408: MOVED from its
original position BEFORE Family 1 to run right AFTER it, so the
now-removed hot-list closure could bind `audit_events` by IDENTITY rather
than a deferred `self._audit_events` NAME lookup — closing the #2856
accident's class, a name reference silently resolving to a DIFFERENT
EventLog than the one live when it was written) no longer has a live
reason attached to THIS family specifically — nothing remaining here reads
`audit_events` at all. The position is left as-is rather than reverted (no
live bug either way; reverting on its own merits, if ever warranted, is a
separate, later change). `tests/repo/test_audit_events_single_assignment_
3408.py`'s AST arm (git-grep evidence: `_audit_events =` is a single
assignment repo-wide, in `Session.__init__` only) remains true and
unaffected by this removal — its own subject was never this family's
attrs, just the identity-vs-name binding discipline generally.

`_universal_wrappers_enabled` (FP-0034 PR-3b-iii; renamed from `_action_retrieval`
and its source moved from the now-deleted `action_retrieval:` block to `tool_use:`,
#4552 PR-3+4) drives whether the universal catalog wrappers appear in the router
`tools=`. A plain bool, `chat_universal_wrappers_enabled: bool = True` by default
(previously an `ActionRetrievalConfig` object; the field simplified to a scalar
along with the same move), so existing chat behaviour is preserved when callers
don't pass one. `_eager_embedding_build`
(B25-S5-1 fix): when `True`, `RouterLoop` awaits the embedding index build synchronously on
the first turn (Turn 1 blocks ~2-5s) so `search_actions` is visible to the LLM from the
very first call; default `False` keeps the lazy background-build path.

### Embedding off-state degrade (FP-0034 Phase 2 step 1)

When `embedding.enabled` is not set (or `universal_wrappers_enabled` is
`False`), `_build_retrieval_bundle` leaves `action_embedding_index`,
`embedding_provider`, and `embedding_model_class` at `None` rather than
constructing stand-in objects (FP-0066 §7 — clean-break replacement for the
retired `action_retrieval.embedding_class` on/off gate; the model CLASS is
`embedding.default_class`).

This `None` state is a contract two OTHER call sites rely on:
- `build_tools` reads it to HIDE the `search_actions` wrapper from the
  LLM-visible catalog entirely when embedding is off.
- The `search_actions` handler degrades to an empty-result response if it is
  ever reached with a `None` index (defense in depth — `build_tools` should
  already have hidden it).

Removing the `None` guard (e.g. always constructing a stand-in
`ActionEmbeddingIndex`) without updating both of those call sites would
surface `search_actions` to the LLM with a `None`/inert index; invoking it
would raise `AttributeError` inside the handler instead of returning the
intended empty-result degrade.

### Embedding event sink — removed (#3438)

FP-0043 Component C.3 originally wired the embedding provider's lazy
model-load lifecycle (downloading / loaded / error) into the session's
events bus via an `_embedding_event_sink` closure (`f"embedding_{kind}"`),
threaded through `Session` -> `OpContext` -> the `embed` op -> the provider
(FP-0057 #2856 Part A) so the TUI could render a sticky status row. #3128
later removed the in-process embedding-model backend that had a lazy-load
lifecycle to report on — reyn depends on litellm exclusively for
embeddings now, and local/offline models are reached (if wanted) via an
operator-run litellm proxy, which reyn does not manage a download
lifecycle for (see
[rag.md § Local and offline embedding models](../../concepts/data-retrieval/rag.md#local-and-offline-embedding-models)).
That left the sink with no producer: `get_provider` only forwards an
`event_sink` kwarg to a provider class whose signature accepts it, and the
sole implementation (`LiteLLMEmbeddingProvider`) never did — so no
`embedding_*` audit-event kind was ever emitted. #3438 (independent
re-verification confirmed the zero-producer finding, and found no
comment/ADR/issue recording an intent to keep the wire for a future
provider) deleted the sink, `OpContext.embedding_event_sink`, and every hop
of the threading, along with the `KIND_FAMILY` registry entry it used to
justify in `event_schema.py`'s `DYNAMIC_KIND_EMIT_SITES`.

**#4552 (2026-08):** the `_on_hot_list_changed` closure this paragraph used
to warn about (a name-capture recurrence risk, C.4 hotfix 2026-05-27) is
removed along with the hot-list feature it belonged to (owner directive:
discarded) — the warning no longer has a live subject.

## Family 6a — Router-waist (`RouterHostAdapter`)

`_build_router_waist` aggregates ~40 already-constructed Session sub-components (Families
1-5's outputs + params/early attrs set earlier in `__init__`) into `RouterHostAdapter`, the
single object most later families read through — a byte-identical extraction (same object,
same construction order, same values, including the DEFERRED per-turn callables —
`live_session_id_inputs.live_session_id_fn` and every `*_fn` field of the op-context
supplier — kept verbatim, still closing over `self` and resolved at call time, not
eager-ized). It stays UNMOVED, invoked at its original position — every dependency is
already set on `self` by this point.

**#3607 op-context supplier**: `_build_router_waist` also builds
`self._router_op_context_source` (a `RouterOpContextSource`, see
`runtime/router_op_context.py`) and hands the SAME object to the adapter as
`op_context_source`. It is the only caller of `build_router_op_context`, so
`Session._make_router_op_context` and `RouterHostAdapter.make_router_op_context`
are both one-line delegations to `.build()` and cannot hand out different
capabilities. It replaced a 16-field `RouterOpContextInputs` bundle that was a
COPY of the Session attributes the Session's own builder read directly — two
argument lists for one object, which had diverged on twelve fields.

**#3482 param bundling**: `RouterHostAdapter.__init__` groups every real
consumer-set cluster (measured by AST, not by name prefix) into frozen,
default-free dataclasses built just before the constructor call — three of
them today (`ROUTER_HOST_ADAPTER_BUNDLE_TYPES`), each named after the sole
consumer the measurement found:

| bundle | fields | sole consumer |
| --- | --- | --- |
| `McpGatewayInputs` | `mcp_connection_service`/`mcp_agent_id`/`ephemeral_fn` (#3447's Path A fold) | `_mcp_list_via_gateway` |
| `PutOutboxInputs` | `put_outbox`/`agent_replies_tracker` | the `put_outbox` method |
| `LiveSessionIdInputs` | `session_id`/`live_session_id_fn` | the `live_session_id` property |

(`SendToAgentInputs` was a fourth bundle here — removed along with the dead
`RouterLoopHost.send_to_agent`/`RouterHostAdapter.send_to_agent` Protocol
member it existed solely to serve, #4144/#4153; `InterAgentMessaging.send_to_agent`,
the still-live P4e delivery transport, is a different, unrelated method.)

Session still builds each field with the exact same expression as before
(same object, same order, same call-time semantics — `ephemeral_fn` /
`live_session_id_fn` and the two tracker lambdas are still live per-turn
callables, not eager-ized); only the wire shape changed.

**#3607 — ask the layering question BEFORE the bundling one.** Four params
(`file_read` / `file_write` / `file_delete` / `file_regenerate_index`) left the
constructor entirely rather than becoming a sixth bundle. They shared a consumer
set, so the measurement above would happily have bundled them — and the result
would have been primitives neatly grouped, with the 57 lines of memory domain
rule still sitting in `RouterLoop` on top of them. The prior question is "is this
a capability, or a primitive the capability is assembled from?": the adapter now
receives `memory` (the `MemoryService`) and the router calls its operations. Note
what the bundling criterion cannot see — a Parameter Object moves parameters to a
common root; only a Facade Service hides the aggregate BEHAVIOUR. The five
bundles below are the former by design (their own docstrings say "no construction
logic"); they are not evil, they are insufficient for a layering defect.

The remaining bare params are bare because **no other param travels to the
same set of destinations** — and that is not recorded anywhere as prose. It is
COMPUTED, by `scripts/measure_router_host_adapter_consumers.py` (exact
consumer-set equality; a member that reads an already-bundled attribute is a
landed bundle's hub and is not counted twice), and enforced by
`tests/runtime/test_router_host_adapter_param_gate_3482.py`, which goes RED when a
bare param acquires an exact-match partner, when a param loses its last
measurable consumer without being shelved in
`ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED`, or when a written claim in either
registry contradicts the measurement. Bundle coverage is deliberately not
100% — forcing it would be name-prefix grouping pressure, not a consumer-set
one. Do not re-record the predicate as a per-param prose reason: a gate can
only check that prose is non-empty, never that it is true — when this was
tried, six such reasons were false, and they are the three bundles at the
bottom of the table above.

### Chat turn_budget engine — None on small context, never raise (#1092 PR-F1)

`_build_router_waist` defines `_build_chat_turn_budget_engine`, a closure
passed to `RouterHostAdapter` as `turn_budget_engine_factory=` — NOT called
at construction (#3671 follow-up: `TurnBudgetEngine.__init__` touches
litellm's model catalog, which used to put that cost on every session's TUI
startup for a value nothing reads until the first force-close check,
mid-turn). `RouterHostAdapter._ensure_turn_budget_engine` calls the factory
at most once, on first reference to `wrap_up_output_reserve` — a lazy,
single-owner cache (`_TURN_BUDGET_ENGINE_UNSET` sentinel, since `None` is
already a legitimate cached RESULT here — see below). When the factory does
run, it resolves the model — `self._resolver.resolve(self.model).model` —
mirroring how `CompactionEngine` resolves a model class before use (the
[#1172](#compactionengine-model-resolved-class-not-the-cosmetic-label-1172)
pattern: never hand the cosmetic class straight through).

`try_build_default_turn_budget_engine` returns `None` — it does NOT raise —
when the model's context window is too small to satisfy the by-construction
force-close floor (`output_reserve + offload_cap < threshold`). A
small-context model is a legitimate chat session that simply cannot support
force-close; it degrades to the pre-force-close path (no cap, no handoff)
rather than failing `__init__` outright. This is exactly why the lazy cache
needs a sentinel distinct from `None`: `None` is this function's own valid
"non-viable" answer, not "not computed yet".

The engine is ADDITIVE: its sole consumer is
`RouterHostAdapter.wrap_up_output_reserve`, inert until the F2 handoff calls
`_force_close_call` — chat stays REACTIVE-only by deliberate per-axis
choice. Changing the builder to raise instead of degrade would make every
small-context-model session fail construction, not just skip one feature.

A `/model` switch rebuilds this EAGERLY, as part of `Session
._rebuild_derived_model_engines_for_model` (the ONE private-Session entry
point the `/model` slash handler calls for BOTH this engine and
`CompactionEngine`'s own rebuild below — folded into a single accessor
rather than exposed as two, per the `_SESSION_RESIDUE` ratchet in
`test_3595_s4_slash_handler_seam.py`; see that method's own docstring).
turn_budget's half stays eager while compaction's half (below) stays lazy
— a deliberate difference, not an inconsistency: by the time `/model`
runs, `maybe_block_high_cost_model` has already touched litellm on this
same call, so `TurnBudgetEngine`'s own touch is a cheap re-touch either way,
and its result feeds a per-turn cap check worth having correct immediately.

## Family 6b — History / compaction

`_build_history_compaction_bundle` constructs `history_buffer` / `compaction_controller`
(including the None-then-patch that breaks their circular dependency) / `budget_advisor` —
a byte-identical extraction, same construction sequence, same position (right after Family
6a's `router_host`, since `history_buffer` eager-depends on it). The session drives
compaction via `force_compact_now()` (pre-frame guard) — background task lifecycle was
removed in #1128 PR-a, all callbacks resolve against `self` at call time. `_token_learner`
(PR-N6) is the adaptive per-user token-estimation learner.

**#4552 (2026-08):** this builder used to also take a `merge_action_usage` param — the
`_merge_action_usage_from_candidates` closure (FP-0019 Wave 1 / #1128 PR-a), a hot-list
compactor sink — threaded through as a nested closure passed to the builder as a callback.
Removed with the hot-list feature it fed (owner directive: discarded).

### CompactionEngine model — resolved class, not the cosmetic label (#1172)

`CompactionEngine`'s `model=` argument is `self.model` — the model CLASS
label (e.g. `"standard"`), NOT a pre-resolved litellm string.
`CompactionEngine.__init__` resolves it itself via its own `resolver`
param — the #1172 guarantee lives INSIDE the engine, not at each call site,
so no construction site (chat / planner / phase) can leak an unresolved
class to litellm by forgetting to resolve first.

Before #1172, the unresolved class was handed straight through: litellm
received `"standard"` literally, which is not a real model identifier, and
`litellm.acompletion` raised `BadRequestError` on the FIRST compaction
trigger for that session — a dead-end-critical failure mode, since
compaction is what keeps long-running sessions inside their context
window.

`#3785` removed the `model_class_by_purpose.compaction` override `#1679`
had layered on top (`self._resolver.purpose_class_or("compaction",
self.model)`): compaction never tracked a `/model` switch mid-session
(nothing ever rebuilt it), so an operator who set a per-purpose compaction
model was silently getting a STALE conversation model instead of either
the compaction override or the current one — the config key described a
guarantee the code never provided. Compaction now always follows
`self.model` directly, and a config that still declares
`model_class_by_purpose.compaction` fails to load
(`config/root.py::_build_model_class_by_purpose`) rather than silently
doing nothing.

#### Construction is lazy, and so is the rebuild (#3671, #3785)

`_build_history_compaction_bundle` defines `_build_chat_compaction_engine`,
a closure passed to `CompactionController` as `compaction_engine_factory=`
— NOT called at construction (#3671 follow-up: `CompactionEngine.__init__`
measures `T_comp_SP` and derives budgets, both of which touch litellm's
model catalog, for a value nothing reads until compaction actually
triggers, mid-turn). `CompactionController._engine` (a property) calls the
factory at most once, on first reference, caching the result — `None` means
"not built yet" here (unlike `TurnBudgetEngine`'s sentinel: `CompactionEngine`
has no legitimate "built but absent" case, so `None` has exactly one
meaning and needs no sentinel).

A `/model` switch reaches this half through the SAME `Session
._rebuild_derived_model_engines_for_model` call as turn_budget's rebuild
above (not a separate accessor — see that method's docstring), which calls
`CompactionController.rebuild_engine()` — this ONLY discards
the cache (`__engine_cache = None`); it does not rebuild eagerly. The SAME
factory closure reads `self.model` fresh on each call, so invalidating the
cache is sufficient: the next real compaction trigger builds against
whatever model is current at THAT moment, not the one active at `/model`
switch time. This stays lazy (unlike `TurnBudgetEngine`'s eager rebuild,
above) because a `/model` switch that never triggers compaction again
should not pay to rebuild it — consistent with #3671's construction-time
discipline.

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
post-waist, reading Family 7's `self._chains` and Family 1's `self._audit_events` eagerly
(both already set by this point) plus a tail of deferred `self.*`/`lambda: self.*` closures
kept verbatim.

## Family 8b — Memory

`_build_memory` constructs the memory-store capability — `MemoryService`, which owns
memory path resolution plus `remember` / `forget` / `read_body` and the domain rules
they carry (the FP-0050 memory-write threat scan, YAML frontmatter, listing-index
regeneration, knowledge-index ingest/de-index via the injected `MemoryKnowledgeSync`).
`RouterHostAdapter` receives it whole and exposes it as `host.memory`; #3607 removed the
adapter's `memory_path` / `memory_dir` delegates and its four `file_*` callbacks, which
existed only so `RouterLoop` could assemble those operations itself.

Position unmoved and PRE-WAIST, before Family 6a's `_build_router_waist` reads
`self._memory` eagerly. Args are eager `self._X` reads except `knowledge_sync`'s
`op_context_fn`, a lambda resolving `self._router_host` at call time (the waist does not
exist yet here, and an OpContext is per-turn state that must not be snapshotted).

## Family 8c — MCP connection service

`_build_mcp_connection_service` — see the builder's own docstring for the full deferred-
resolution crux (4 lambdas resolving `self._audit_events`/`self._router_host`/
`self._hook_dispatcher`/`self._interventions` at CALL time, none of which exist yet at this
position in `__init__`).

### MCPConnectionService — four deferred lambdas over not-yet-built siblings (#2597)

`_build_mcp_connection_service` (Family 8c) constructs the session-owned
held-open MCP connection service (Option C: one persistent `MCPClient` per
server, reused across chat turns/tasks for the session's whole lifetime).
Construction is unconditional and cheap — an empty dict until the first
`get()`.

**Ephemeral routing.** Only the non-ephemeral MCP call sites
(`Session._mcp_call_tool` and, on `RouterHostAdapter`,
`mcp_list_servers`/`mcp_list_tools`/`mcp_list_resources`/
`mcp_list_resource_templates`/`mcp_list_prompts` — #3447 folded the five
listing methods off `Session` onto the adapter itself, threading this same
`self._mcp_connection_service` instance through as a raw constructor
argument) route through this held-open service. An ephemeral session
(`self._ephemeral`, set post-construction via `Session.mark_ephemeral()`
— #5336: a genuine public seam, not a private-name write from outside —
by the registry once spawn mode is known — read via a LIVE `ephemeral_fn`
callable on the adapter side, not
a snapshot, for the same reason `session_id`/`ephemeral` are read through
live providers elsewhere in this doc) keeps using the per-call
`MCPClientPool` instead, so a sub-second-lived spawned session never holds
a server connection open needlessly. The service is closed at session
teardown via `aclose_mcp_connections` (`registry.remove_session` /
`archive_agent`'s main-session path).

**Deferred lambdas.** Three of the six constructor arguments are lambdas
that defer resolution to CALL time, because none of the attributes they
close over exist on `self` yet at this point in `__init__`:
- `emit_sink` / `tools_cache_invalidate` defer `self._audit_events` /
  `self._router_host` (both assigned later in `__init__`) — mirrors the
  `emit_event=lambda et, **d: self._audit_events.emit(et, **d)` pattern used
  elsewhere.
- `hook_trigger` defers `self._hook_dispatcher` (same H1 pattern
  `_build_hook_event_bundle`'s `fs_watcher` uses).

None of the three is ever CALLED until a held MCP connection actually
receives a server-pushed notification, long after `__init__` has
finished — but eager-binding any of them here (e.g.
`tools_cache_invalidate=self._router_host.invalidate_mcp_tools_cache`)
raises `AttributeError` immediately, since `self._router_host` does not
exist yet at Family 8c's position in `__init__`.

`elicitation_gate` uses the SAME `consent_bus`/`consent_gate` split #2095's
shell-hook consent already uses: a server→client elicitation only routes
through this session's `RequestBus` when a live intervention listener is
attached; headless (no listener) auto-declines inside the handler
(`reyn.mcp.elicitation`), never here. `elicitation_bus=self.as_request_bus()`
is safe to call EAGERLY (unlike the deferred lambdas) — it only wraps
`self` in an adapter and reads nothing constructed later.
`agent_name=self.agent_name` is likewise eager, since `self._agent` is
already set earlier in `__init__`.

## Capability, permission & visibility

- `_available_skills` (#2548 PR-A) — enabled skill registry snapshot backing the system prompt's `## Skills` block. `None` → the block is omitted from the prompt entirely, never rendered empty.
- `_skill_collisions` (#3100 Axis 4) — same-name-across-config-tiers collision map, consulted by `:skill` invocation (`reyn.interfaces.skill_invoke`) to fire a LOUD audit-event + warning instead of silently letting one skill definition shadow another.
- `_exclude_tools` (#187) — tool names excluded for the MAIN chat `RouterLoop`, threaded to
  the loop construction below. General capability (mirrors the sub-loop `exclude_tools`,
  `planner.py:1136`); the faithful SWE-eval excludes `web_search`/`web_fetch` so the agent
  solves from the repo + issue, not a web lookup of the gold solution. #3378: this is **not**
  a separate advertisement axis — `RouterLoop` composes it into `_contextual_permission` as
  one more restrict-only ∩ term, so it reaches the LLM-visible catalog and the live
  `tool_excluded` gate through the same field (before, an explicit contextual discarded it
  from enforcement while it still filtered the catalog).
- `_contextual_permission` (#1827 S3) — per-session `capability_profile` narrowing
  (`ContextualPermission`) resolved from the agent's topology role. Threaded to the live
  tool gate (`RouterLoop`) + control-IR `OpContext`. `None` = no narrowing (byte-identical).
  #3378: it is also the source the LLM-visible catalog is filtered by, so a tool this
  narrowing denies is never advertised in the first place — see
  [Capability profile § Advertisement and enforcement read one source](../../concepts/runtime/capability-profile.md#advertisement-and-enforcement-read-one-source).
  `_untrusted_contextual_cache` (#1827 S4b, context-auto) is a lazily-resolved minimal
  `_untrusted` profile `ContextualPermission`, composed into the per-turn narrowing while
  untrusted external content is live in context; `None` until first needed — and it is
  never resolved at all unless the operator opted in via
  `safety.threat_scan.capability_narrowing` (#3501; default `off`). See
  [Capability profile § Context-auto untrusted narrowing](../../concepts/runtime/capability-profile.md#context-auto-untrusted-narrowing-opt-in-default-off).
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
  name is in this set **only when that hook's most-specific origin is `per-agent` or
  `per-session`** (#5213) — a `startup`- or `runtime`-origin hook is NOT skippable this
  way regardless of `_disabled_hooks`' contents (those two layers are outside every
  agent's write zone; disabling them here would grant power the agent does not otherwise
  have). `Session.set_hook_enabled` (#5230) refuses to even ADD a protected hook's name to
  this set — a `disable` request for one changes nothing and is reported as refused, not
  silently recorded-but-inert. Per-session by construction: each `Session` owns its own
  dispatcher + this set, so disabling a hook in session S1 does NOT affect S2 (even though
  the hook config is shared). Persisted to the per-session `hooks.yaml`'s `disabled:` key
  (step2).
- `_spawned_tasks` (#2103 S1bc-exec) — sid → original-task record for sessions THIS session
  spawned. When a spawned session's result routes back, the result header renders
  `task=<the spawner's OWN request>` from THIS trusted record (keyed by the spawned sid) —
  never the spawned session's echo (which a compromised sub-session could forge into
  trusted framing). Bounded-by-construction: evicted on result arrival; a max-size cap
  (evict-oldest) caps a never-arriving result.

### sandbox_backend gate reads the injected instance, not the config string (#1417 / FP-0034)

`exec`/`sandboxed_exec` is always discoverable in the universal catalog
(#4932, owner ruling 2026-08-19 — the earlier D14-ext visibility gate
that could hide it entirely is retired). What still must read the
INJECTED backend's real identity, not the `reyn.yaml` `sandbox.backend`
config STRING, is the ISOLATION-DISCLOSURE text the tool's description
carries: it must name the backend that will actually run the command,
not the config value that may no longer match it.

`sandbox_backend=_exec_gate_backend_name(self._sandbox_backend,
self._sandbox_config)` reads `self._sandbox_backend` — the SAME object used
for actual exec (`tools/exec.py`, #3226 Phase 3 renamed from
`sandboxed_exec.py`: `ctx.sandbox_backend or get_default_backend(...)`).
Both injected backend types (`DockerEnvironmentBackend`, `SandboxBackend`)
expose `.name`.

**The construction-forwarding-gap this closes**: without reading the
injected instance, `sandbox.backend=noop` config plus an injected exec
backend (e.g. `--env-backend=docker`) would (pre-#4932) HIDE exec from
discovery, or (post-#4932) wrongly disclose "no isolation" even though
`sandboxed_exec` is functionally isolated through the injected backend —
either way the mismatch is silent, indistinguishable from an operator who
deliberately configured `noop`. No injected instance → falls back to the
config string (auto / host-default behaviour unchanged).

### available_skills_fn reads the BASE set, not the UX-filtered copy (#3196)

`RouterHostAdapter` receives two related-but-distinct skill sets:

- `available_skills=self._available_skills` — the base registered-skill set
  (#2548 PR-A).
- `available_skills_fn=lambda: self._available_skills` on the op-context
  supplier — a LIVE read of that SAME `Session._available_skills` field.

The reason for the second, seemingly-redundant source: `RouterHostAdapter`
holds its OWN `_available_skills` attribute, which `reapply_skill_visibility`
mutates into a UX-filtered COPY (skills the user toggled off via the status
bar disappear from it, `#2285`). The router OpContext uses
`available_skills_fn` for the `file` op's skill-load provenance gate — a
TRUST decision, not a UI-visibility decision.

If a future edit swaps `available_skills_fn` to close over
`RouterHostAdapter`'s own (filtered) `_available_skills` instead of
`Session._available_skills`, the provenance gate would start following the
UX visibility toggle: a skill a user merely HID from their own view (but is
still authorized to use) could fail a trust check it should pass, or vice
versa. Neither failure raises an exception; it only shows up as a wrong
trust decision on a specific skill, discovered later if at all.

## Multimodal / media

- `_multimodal_config` (#364) — media-size gate config plumbed through to spawned Agents
  AND to the router host adapter (chat-router `web_fetch`/`read_file`/mcp paths).
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
- `media_store_worker` (#5382 example②) — an optional `DurabilityWorker` forwarded
  straight into `MediaStore(worker=...)`. `None` (the default) leaves `MediaStore`'s own
  lazy-default in place — a dedicated, unshared worker per Session, unchanged from before
  this param existed. A caller passes one explicitly to give multiple sessions in one
  process a single shared write-serialization point. `MediaStore(worker=...)` already
  existed (#5364 §1.4); Session simply wasn't threading a caller-supplied one through — this
  is the one construction input added, not a general override seam (an `overrides=...`
  catch-all was considered and rejected on #5382: no boundary, and it would undo #3133's
  45→36 param-surface cut with one opaque param).
- `_pending_user_attachments` (#366, widened to any file type by #5509) — queue of media
  blocks the user attached via `/image PATH` (images only) or `/attachment PATH` (any file
  — mime resolved via stdlib `mimetypes`, never a reyn-specific table), drained on the next
  user-message turn (attached to that `ChatMessage`'s `media` field). Each block's own
  `"type"` (`image`/`document`/`file`/`video_url`/`audio`) is DERIVED from its `mime_type`
  via `router_loop.classify_media_block_type` — never independently chosen by a producer
  (#5526 — closes the risk of a block's `type` and `mime_type` disagreeing). Path-ref
  shape (#383 PR-C — the file itself is the source of truth, never duplicated into
  `history.jsonl`): `{"type": ..., "path": ..., "mime_type": ..., "content_hash": "sha256:..."}`.
  Materialised at wire-build time into the matching litellm content part (`image_url` /
  `document` / `file` / `video_url` / `input_audio`) — see `router_loop.
  build_wire_media_part`.
- `_web_fetch_config` (#4274) — `reyn.yaml` `web_fetch.*` (SSL verify / private-IP
  opt-in / download-byte cap), plumbed straight into the router `OpContext.web_fetch_config`
  the `web_fetch` op reads. Same shape as `_multimodal_config` above (a plain value, not a
  per-turn supplier — `WebFetchConfig` doesn't change mid-session). Threaded from
  `SessionFactoryConfig.web_fetch_config` (#2093 bundle), so it reaches all five
  session-factory sites uniformly; before #4274 the field existed on `OpContext` (#4174 T4)
  but no factory site ever populated it, so every real session silently ignored an
  operator's `web_fetch:` block.

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
- **RETIRED (#5561, owner ruling, 2026-08-30)**: `_hook_driven_turns` (#1800
  slice 7) — the hook-driven-turns loop-valve counter that bounded hook
  self-continuation, snapshot-backed since #2884. No operator could derive
  a correct cap value for it (owner, verbatim: "hook 起動を回数で制限なんて
  誰も設定できないでしょ。どんな回数が妥当か誰も判断できない"), so the
  field, its WAL kind (`hook_driven_turns_set`), the `AgentSnapshot` field,
  and the enforcement site were all removed together. Replaced by
  `CostConfig`, #5516's N-into-one push folding, and per-push size bounds —
  see `LoopConfig`'s own docstring, `config/chat.py`, for the full
  rationale. An old on-disk snapshot/WAL still carrying either the field or
  the kind is tolerated by `AgentSnapshot._apply_one`'s existing "unknown
  kinds: no-op" fallback (the same reader-tolerance #3436 established for
  `task_subscribed`/`task_rebound`'s own retirement).
- `next_turn_context` (#1800 slice 4b; moved off `Session` onto `InboxArbiter` in `#3978` P1, née `_next_turn_context`) — in-memory staging buffer for `wake=false`
  ride-along (C) messages drained by `InboxArbiter.drain_to_wake`. Entries are applied to the next
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
  (`run_one_iteration`). Read by the router op-context supplier so `task.create` derives
  ownership (`requester=<this task>`). `None` = not executing an assigned task (a user /
  hook / recovery turn). Slice B extends the lifetime to a persistent assignment spanning
  continuation + recovery turns.
- `_current_turn_origin` (proposal 0060 Phase 1 Layer A, A7) — the OS-authoritative
  provenance classification of the turn currently being processed, mirroring
  `_current_task_id` exactly (same seam, same threading). Set per-turn in
  `_stamp_execution_context`; read per build by the router op-context supplier so install-op handlers
  (skill/pipeline/present, A9) stamp `entry["provenance"]` from a single OS-set source the
  LLM cannot spoof. Initialized to the STRICTER value (fail-safe: never default to
  `"user_directed"` before the first turn is classified).
- `_ephemeral` / `_vanish_scheduled` / `_vanish_task` (#2103) — a spawned EPHEMERAL session
  (spawn-time `mode="ephemeral"`) auto-vanishes once its task is done. `_ephemeral` set
  post-construction via `Session.mark_ephemeral()` (#5336: a genuine public seam,
  not a private-name write from outside) by the registry on an ephemeral spawn;
  `pipeline_executor_driver.py` also calls it, much later, to reuse the existing
  auto-vanish mechanism as a "vanish now" trigger for a session that was not
  originally ephemeral — the two call sites are NOT the same moment (architect
  finding, #5336), which is why a setter method exists instead of a
  constructor-time argument. The main session + persistent
  spawns leave `_ephemeral` `False`. `_vanish_scheduled` guards against a double-schedule
  across turns.

## Misc lifecycle wiring

- `_STATE_CHANGE_EVENT_MAPPINGS` per-entry rationale (why each op-emitted event is worth a `state_change` line, not just that it is one):
  - `mcp_server_installed` (`mcp_install` op) — the LLM otherwise has no way to learn a newly-installed server exists until it re-lists tools; surfacing it as `state_change` lets it use the new server within the same conversation without an explicit re-check.
  - `mcp_server_removed` (`mcp_drop_server` op) — symmetric to `mcp_server_installed`: surfaces the 'no longer available' state-change so the LLM stops retrying calls against a removed server instead of discovering the failure only on next invocation.
  - `index_dropped` (`index_drop` op) — recall against the dropped source will now miss; surfacing the change lets the LLM understand a source it cited earlier in the conversation no longer exists, rather than treating a later empty-recall result as a retrieval bug.
- `_spawn_tracker` (`SpawnTracker`, `#2103`, see `#3133` P3 Extract Class) — owns the spawned-task correlation record + ephemeral auto-vanish scheduling state; Session holds one reference and delegates via thin forwarders rather than re-owning the state (see `spawn_tracker.py`). Its `session_id`/`ephemeral` inputs are threaded as LIVE providers (`lambda: self._session_id`, etc.), not snapshotted values — both are reassigned post-construction by the registry (spawn-time re-key, ephemeral-spawn flip), so a value captured at `SpawnTracker` construction time would go stale the moment the registry re-keys or flips it. `CapabilityVisibility` (constructed later in `__init__`) faces the identical hazard for its own `session_id_provider` and uses the same live-lambda fix — see that construction site for the mirrored pattern.
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
- `_on_audit_event_for_state_change` (#398 v4 emitter family) — a generic events-log
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
