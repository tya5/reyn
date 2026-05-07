# ADR-0022: Plan-Mode Crash Resilience — Phase 1 (Fail-Safe + Observability)

**Status**: Accepted (2026-05-07)
**Track**: Plan-mode crash recovery (= step toward Phase 2 forward replay,
ADR-future)

## Context

Plan-mode (= chat router's `plan` tool, landed at commit `6b41fd0`) lets
the LLM decompose a complex query into 2-7 sub-steps, executed
sequentially in narrow child `RouterLoop` instances. The terminal step's
captured text becomes the user-facing reply.

Plan execution is currently **fully ephemeral**:

- `Plan` artifact, `step_results: dict[str, str]`, `step_failures: dict`,
  in-progress `_PlanStepHost.captured_text` — all in-memory only.
- No WAL events for the plan lifecycle as a unit (only per-step audit
  events emitted to the events log).
- No `AgentSnapshot` field tracking in-flight plans.

A crash mid-plan therefore loses **all** progress and, critically:

- Any **child skill** spawned by a plan step (via `invoke_skill`) is
  written to disk (`SkillRegistry`) and will auto-resume on next
  startup. The parent plan has gone, so the skill becomes an
  **orphan** delivering its reply to nowhere.
- If the user re-issues the query, the LLM may re-plan and re-spawn the
  same child skill (= **duplicate spawn**, duplicate side effects, double
  LLM cost).

This is structurally inconsistent with the rest of Reyn: skills, chains,
and interventions all have crash-recovery stories (PR21, R-D14, R-D12).
Plan-mode is the only first-class chat router primitive that loses
state on crash.

## Considered alternatives

- **A. Defer entirely.** Document plan-mode as "MVP, retry on crash"
  and wait for user-visible blast-radius reports. Rejected: silent
  duplicate-spawn already happens, even without user reports.
- **B. Full forward-replay (= skill-resume parity).** Per-plan snapshot
  + analyzer + coordinator + `PlanRuntime`. Mirror the ADR-0002
  architecture entirely. Rejected for now (= will become Phase 2):
  scope is multi-week, requires `reyn.yaml` policy schema change, and
  leaves an open coordination question with already-resumable child
  skills (= adoption vs cancel) that benefits from real operator data
  before being decided.
- **C. Phase 1 fail-safe + observability only.** Add WAL events for
  plan lifecycle, track in-flight plans on `AgentSnapshot`, and on
  restart **discard** orphan plans + cancel their child skills with a
  user-facing notification. **Accepted.** Net effect: silent failure
  becomes loud and graceful failure. Step results are not preserved,
  user reissues the query.

## Decision

**Adopt C — Phase 1 fail-safe + observability.** Plans become
**event-traceable and crash-discoverable** without yet introducing per-
plan snapshots or forward replay.

### Persistent surface added

1. **WAL event kinds** (additive to `state_log.WAL_EVENT_KINDS`):

   ```
   plan_started, plan_completed, plan_aborted
   ```

   `plan_started` fires at the top of `execute_plan`. `plan_completed`
   fires on normal return or `WorkflowAbortedError`. `plan_aborted` fires
   on AgentRegistry-side cleanup post-restart for orphan plans.
   Per-step audit events (`plan_emitted`, `plan_step_started/completed/
   failed`, `plan_aggregated`) **stay in the events log**, not in the
   WAL — they are forensic, not recovery state.

2. **`AgentSnapshot.active_plan_ids: list[str]`** — additive field,
   default `[]`. Apply handlers:

   ```
   plan_started   → append plan_id (no-op if present)
   plan_completed → remove plan_id
   plan_aborted   → remove plan_id
   ```

   **No `SNAPSHOT_VERSION` bump.** The field follows the
   `parent_run_id` (R-D13) precedent: `data.get("active_plan_ids", []) or []`
   on `load`. Operators do not need `--reset` to upgrade.

3. **`SnapshotJournal` methods** (mirror `record_skill_started` shape):

   ```
   async def record_plan_started(plan_id, goal, n_steps)
   async def record_plan_completed(plan_id)
   async def record_plan_aborted(plan_id, reason)
   ```

   Each appends to `state_log` (target=agent_name) and persists the
   updated `AgentSnapshot.active_plan_ids` via the existing atomic save.

### Runtime changes

4. **`execute_plan` finally clause** mirrors ADR-0013's runtime pattern:

   ```python
   plan_id = uuid4().hex[:8]
   await journal.record_plan_started(plan_id, ...)
   try:
       # existing step loop
   finally:
       exc_type = sys.exc_info()[0]
       if exc_type is None or issubclass(exc_type, WorkflowAbortedError):
           await journal.record_plan_completed(plan_id)
       else:
           parent_host.events.emit(
               "plan_run_interrupted", plan_id=plan_id,
               exc_type=exc_type.__name__,
           )
           # active_plan_ids stays populated → restart cleanup will discard
   ```

   `kill -9` and SIGKILL bypass `finally` entirely; in that case the
   `plan_started` WAL entry persists with no matching `plan_completed`,
   which is exactly what restart cleanup detects.

5. **`AgentRegistry.restore_all` plan cleanup** (post-WAL-replay,
   post-snapshot-save):

   For each agent with non-empty `active_plan_ids`:
   - For each plan_id, find spawned child skill_run_ids by scanning
     events log entries `kind=plan_step_completed` carrying child
     references (via the `chain_id` correlation already emitted by
     `_PlanStepHost.run_skill_awaitable`'s downstream events).
   - For each child skill_run_id still in `AgentSnapshot.active_skill_run_ids`:
     `skill_registry.complete(run_id, status="discarded")` and emit
     `chain_peer_discarded`-style notification (= reuse R-D14 path).
   - `journal.record_plan_aborted(plan_id, reason="restart_cleanup")`.
   - Surface user-visible message via `session.put_outbox(
     OutboxMessage(kind="error", text=...))`.

   The cleanup is best-effort (= defensive, swallows per-plan errors
   with a warning). A failed cleanup leaves the plan_id orphaned but
   does not block resume of other state.

### Explicit non-goals (= Phase 2 territory)

- **Step result preservation.** Phase 1 discards `step_results` at
  crash. The user re-issues the query; the LLM re-plans (likely
  identically). Cost: LLM tokens are spent again. Phase 2 will
  introduce per-plan snapshots to memoize completed steps.
- **Mid-step resume.** A plan step that crashed mid-`invoke_skill` is
  fully redone in Phase 2; in Phase 1 the child is cancelled and the
  user retries.
- **`reyn.yaml` `plan_resume:` policy.** Phase 1 has one fixed policy:
  discard. Phase 2 adds operator-tunable `retry_pending_steps` /
  `discard_plan` / `resume_from_step`.
- **`PlanRuntime` peer to `OSRuntime`.** Phase 2 introduces this as a
  proper runtime entry; Phase 1 keeps `execute_plan` as the inline
  executor it is today.

### chain_id alignment

Plan-mode runs **inside the chat turn's chain_id** (= the user message's
chain). Plan steps that spawn skills via `invoke_skill` allocate their
**own** child chain_id (existing `run_skill_awaitable` path). Phase 1
does not register a separate chain for the plan itself — the user is
already the implicit waiter via the outbox. R-D14's
`notify_chain_discarded` therefore does **not** fire for plan abort;
plan-cleanup uses `put_outbox` directly.

## Consequences

### Positive

- **Silent failure → loud failure.** User sees an explicit "plan
  interrupted, please retry" outbox message instead of a missing
  reply.
- **No duplicate side effects on retry.** Child skills are explicitly
  cancelled before user retry, so the LLM's next invocation starts
  from a clean slate.
- **Plan lifecycle is now traceable in WAL.** Forensics (= "did this
  plan crash or did the user just close chat?") is answerable.
- **Substrate for Phase 2.** The events Phase 1 emits are exactly
  what Phase 2's analyzer will consume.
- **Schema-compatible.** Field is additive; no `--reset` required.

### Negative

- **No work preserved.** If a plan completed 4 of 5 steps and crashed
  on step 5, all 4 are wasted. For long-running plans this is
  meaningful cost.
- **User must re-issue.** UX is "your last query was interrupted,
  please retry" — not great, but honest.
- **Cleanup logic adds AgentRegistry surface.** Already-defensive
  registry now does additional work post-restore.

### Open issues (= deferred to follow-up or Phase 2)

- **`running_plans` task tracking on `ChatSession`** (parity with
  `running_skills`). Phase 1 plans are awaited inline in the dispatch
  call, so `/skill discard <id>`-equivalent for plans is not
  immediately needed. If user-driven cancellation becomes useful,
  Phase 2 introduces this.
- **WAL truncation floor for plans.** Phase 1 plans complete or
  abort within a single chat turn (= short-lived), so the existing
  `_compute_truncate_floor` calculation does not need plan-aware
  modification yet. Phase 2's `PlanSnapshot` will require it.
- **Multi-process plan lifecycle.** Out of scope (cross-process
  coordination is not a Reyn target — see ADR-0001).

## Implementation surface (= concrete files touched)

- `src/reyn/events/state_log.py` — append `plan_*` to `WAL_EVENT_KINDS`.
- `src/reyn/events/agent_snapshot.py` — add `active_plan_ids` field +
  apply handlers, additive on load.
- `src/reyn/chat/services/snapshot_journal.py` — three new
  `record_plan_*` methods.
- `src/reyn/chat/planner.py` — allocate `plan_id`, wrap `execute_plan`'s
  step loop in try/finally, emit lifecycle records via the parent host's
  journal accessor.
- `src/reyn/chat/router_loop.py` — `RouterLoopHost` Protocol gains an
  optional plan-lifecycle accessor (or `dispatch_plan_tool` receives an
  explicit lifecycle callback). Resolve so `_PlanStepHost.parent` does
  not need to know about journal directly.
- `src/reyn/chat/registry.py` — `restore_all` post-replay plan cleanup
  hook.
- `tests/test_planner.py` (extended) + new
  `tests/test_plan_lifecycle_crash.py` (Tier 2 invariants:
  `plan_started` → `plan_completed` round-trip, finally-clause
  exception classification, restart cleanup discards orphans, child
  skill cancel reuses R-D14).

## Cross-references

- **ADR-0001** (state model — WAL + snapshot): plan-mode now also
  participates. Update `0001` with a one-paragraph note pointing at
  this ADR. Phase 1 explicitly avoids the WAL-truncation-floor
  modification ADR-0001 mandates for active runs (= plans are
  short-lived in Phase 1; revisit for Phase 2).
- **ADR-0002** (forward-replay resume): the eventual Phase 2 ADR will
  be a peer of ADR-0002 covering plan-mode replay.
- **ADR-0013** (runtime crash lifecycle): Phase 1's finally pattern is
  the same exception-aware classification (`WorkflowAbortedError` →
  complete, generic `Exception` → preserve for cleanup).
- **ADR-0018** (cross-agent discard notify): R-D14's
  `notify_chain_discarded` is **not** reused for plan abort — plans
  notify the user directly via outbox. Document this as a deliberate
  divergence: chains have peer agents waiting; plans only have the
  end user waiting.

## Future work

A follow-up ADR (working title `Plan-Mode Forward Replay`) will cover:

- `PlanSnapshot` dataclass (mirroring `SkillSnapshot`) with
  `step_results` persistence.
- `PlanResumeAnalyzer` / `PlanResumeCoordinator` mirroring the
  skill-resume primitives.
- `PlanRuntime` as `OSRuntime` peer (= not a special case of either).
- Coordination policy for plan steps that spawned child skills: the
  child is independently resumable, so "adopt vs cancel" must be a
  config-driven decision per child purity classification (ADR-0003
  taxonomy applies).
- `reyn.yaml` `plan_resume:` policy.
- Decomposition output as workspace artifact (= P5 invariant; do not
  re-decompose on resume).
