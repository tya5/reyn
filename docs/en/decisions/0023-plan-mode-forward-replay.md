# ADR-0023: Plan-Mode Crash Resilience — Phase 2 (Forward Replay)

**Status**: Accepted + Implemented (2026-05-07)
**Track**: Plan-mode crash recovery — successor to ADR-0022 Phase 1.
**Synthesized from**: 4 parallel design proposals (snapshot / analyzer / policy
/ runtime), authored 2026-05-07.

**Landing**: 7-step migration completed across commits `bcf1105`
(decomposition artifact) → `65160e3` (PlanSnapshot) → `c1a953d` (PlanRegistry)
→ `c2c3b24` (plan_step_* WAL promotion) → `488bfa9` (PlanRuntime thin wrapper)
→ `c58840e` (dispatch_plan_tool migration) → `f1d81e3` (analyzer + memo replay)
→ `5279341` (coordinator + reyn.yaml policy) → `1e529d7` (ChatSession +
AgentRegistry integration). 1279 → 1373 passed (= 94 new Tier 2 tests).
Phase 1 tests (10) + planner tests survived unchanged.

## Context

ADR-0022 landed Phase 1: plan-mode is now **crash-discoverable** via
`plan_started` / `plan_completed` / `plan_aborted` WAL events plus
`AgentSnapshot.active_plan_ids`. On restart, `AgentRegistry.restore_all`
detects orphan plans, records `plan_aborted`, and surfaces a
"please retry" outbox message. Step results are **not preserved**; the
user re-issues the query.

Phase 1's explicit non-goals (from ADR-0022) are this ADR's scope:

- Step result preservation across crash
- Mid-step resume
- `reyn.yaml` `plan_resume:` policy schema
- `PlanRuntime` peer to `OSRuntime`
- Coordination policy for plan steps that spawned child skills (=
  adopt vs cancel)
- Decomposition output as workspace artifact (P5 invariant)

Phase 2 also addresses the deferred WAL-truncation question (= long-
lived plans cannot block the truncation floor without participating in
the floor calculation).

## Considered alternatives

- **A. Defer Phase 2 indefinitely.** Rejected: the no-step-result-
  preservation property of Phase 1 is a real cost on long plans (=
  user re-pays LLM tokens for already-completed steps).
- **B. Treat plan-mode as a special skill.** Implement plan as a
  stdlib skill with skill_resume infra reused verbatim. Rejected per
  the audit recommendation (Section 8 of impl audit): skills are a
  static-graph + LLM-decided-transitions abstraction; plans are flat-
  list + deterministic-execution. Forcing plan into the skill
  abstraction would bend P4 (LLM picks `next_phase` from candidates)
  invariants.
- **C. "Same primitives, separate runtime entry."** Reuse WAL kinds,
  snapshot atomic-write recipe, schema versioning, analyzer/coordinator
  patterns; introduce a new `PlanRuntime` as `OSRuntime` peer (not
  subclass). **Adopted.**

## Decision

Adopt design **C**, implemented as **5 sub-decisions** corresponding
to the 4 sonnet design proposals plus the migration path.

### 3.1 PlanSnapshot + persistence

#### Dataclass shape

`PlanSnapshot` mirrors `SkillSnapshot` with steps in place of phases:

```
PlanSnapshot:
  plan_id: str                              # uuid4-hex[:8] (Phase 1 precedent)
  agent_name: str
  chain_id: str                             # parent chat-turn chain
  goal: str                                 # original user query (= plan.goal)
  applied_seq: int = 0                      # ADR-0001 watermark
  last_step_applied_seq: int = 0            # WAL truncation gate
  schema_version: int = PLAN_SNAPSHOT_VERSION  # = 1 (ADR-0006)
  decomposition_artifact_path: str | None = None  # P5 canonical source (§3.5)
  steps_serialized: list[dict] = []         # inline fallback (§3.5)
  step_results: dict[str, str] = {}         # memoized step text outputs
  step_failures: dict[str, str] = {}        # per-step error reprs
  current_step_id: str | None = None        # forward-replay anchor
  last_committed_step_id: str | None = None # mirror of skill field
  spawned_skill_run_ids: dict[str, str] = {}  # step_id → child_run_id
  parent_skill_run_id: str | None = None    # ADR-0017 lineage analog
  usage_tokens_so_far: dict | None = None   # optional cost bookkeeping
```

`PLAN_SNAPSHOT_VERSION = 1`. **No `AgentSnapshot.SNAPSHOT_VERSION` bump**
— `active_plan_ids` from Phase 1 is unchanged; the new file is parallel
infrastructure.

#### Persistence path

```
.reyn/agents/<agent_name>/state/plans/<plan_id>.snapshot.json
```

Atomic save via `tmp + fsync + rename` (= verbatim `SkillSnapshot.save`
recipe). Sibling directory to existing `state/skills/` so glob
semantics don't collide.

#### Lifecycle

- **Create**: at `plan_started` (= Phase 1 already emits this WAL kind).
- **Update**: after each `plan_step_completed` / `plan_step_failed`.
- **Delete**: on `plan_completed` / `plan_aborted` (= mirror
  `SkillRegistry.complete` ordering: WAL append first, then
  `unlink(missing_ok=True)`).

#### WAL truncation floor extension

`AgentRegistry._compute_truncate_floor` reads each per-agent `plans/`
directory directly (= JSON parse, not dataclass load), collects
`last_step_applied_seq` from each, and includes them in the floor
calculation. **Direct read** (= same shape as the existing skill block
at `registry.py:557`) sidesteps `PLAN_SNAPSHOT_VERSION` bumps blocking
truncation.

`PlanRegistry` bumps `last_step_applied_seq` on:
- `plan_started` (= initial stamp)
- `plan_step_completed` (= durable progress)
- `plan_step_failed` (= conservative; failure is real progress that
  shouldn't be replayed without policy intervention)

`plan_step_started` does **not** bump the watermark (= mirror
`step_started` for skills).

### 3.2 PlanResumeAnalyzer + PlanResumePlan

#### PlanResumePlan output shape

```
PlanResumePlan(frozen):
  plan_id: str
  chain_id: str
  goal: str
  n_steps: int
  decomposition_artifact_path: str | None
  step_states: list[PlanStepState]    # one per declared step, topo-ordered
  has_ambiguity: bool                 # any step in interrupted_with_child
  has_in_flight_child: bool
```

#### PlanStepState — 4-state union

```
PlanStepState(frozen):
  step_id: str
  started_seq: int | None
  state: Literal[
    "pending",
    "completed_with_result",
    "failed",
    "interrupted_with_child",
  ]
  # state-conditional payload
  result_text: str | None              # completed_with_result
  error_kind: str | None               # failed
  error_message: str | None            # failed
  child_run_id: str | None             # interrupted_with_child
  child_state: Literal["completed","in_flight","discarded","unknown"] | None
  # common
  n_attempts: int = 1
  is_effectful: bool                   # derived from step.tools
  step_signature: str                  # hash of (description, tools, depends_on)
                                       # for decomposition-drift detection
```

#### Pairing algorithm

`(plan_step_started, plan_step_completed)` paired by `(plan_id,
step_id)`, FIFO per-key (= mirror `SkillResumeAnalyzer`'s
`(op_invocation_id)` queue pairing). The 4 outcomes per `(plan_id,
step_id)`:

| WAL pattern | Resulting state |
|---|---|
| no `plan_step_started` | `pending` |
| `started + completed` | `completed_with_result` |
| `started + failed` | `failed` |
| `started`, no terminal, child spawned | `interrupted_with_child` |
| `started`, no terminal, no child | `pending` (pure/world) OR `failed("ambiguous_no_terminal")` (effectful) |

#### Step purity from `step.tools`

Lightweight derivation (= no separate purity registry): the analyzer
maps each name in `step.tools` against the existing `OP_KIND_REGISTRY`
purity field; the step's overall purity is the **highest** purity tier
present (`pure < world < side_effect < external < llm`).

`invoke_skill` is special-cased: it escalates to
`interrupted_with_child` on incomplete pairing, because the spawned
child has its own resume infrastructure that the coordinator must
reconcile (§3.3).

#### WAL event channel

Phase 2 promotes `plan_step_started` / `plan_step_completed` /
`plan_step_failed` from forensic-only (= events log per Phase 1) into
the **WAL** (= `WAL_EVENT_KINDS`-registered, replay-stable). This is
required for analyzer determinism across restart.

`plan_emitted`, `plan_aggregated`, and `plan_run_interrupted` remain
**forensic-only** (= events log, not WAL) — they don't drive resume
decisions.

#### child_skill_lookup dependency

`PlanResumeAnalyzer.analyze(*, snapshot, decomposition, wal_events,
child_skill_lookup)` takes a callable `child_skill_lookup:
Callable[[str], ChildSkillState]` injected by the runtime (= a
`SkillRegistry` query function). The analyzer stays P7-pure: no direct
import of `SkillRegistry`.

### 3.3 PlanResumeCoordinator + reyn.yaml schema

#### Top-level actions

```python
PlanResumeAction = Literal[
  "resume",          # all steps complete, finalize aggregation
  "retry_pending",   # memo committed steps, re-execute pending
  "discard",         # abort, emit plan_aborted, cancel children
  "prompt_required", # reserved type-level value, NOT produced from policy
]
```

`prompt_required` is kept in the type system (mirror
`SkillResumeCoordinator` precedent) but **no policy path produces it
in Phase 2**. ADR-0012's "no prompt at restart" carryover applies (§3.7).

#### Per-purity child action

```python
PlanResumeChildAction = Literal["adopt", "cancel"]
```

#### reyn.yaml schema

```yaml
plan_resume:
  default: retry_pending_steps   # one of: retry_pending_steps | discard_plan
  child_purity:
    pure:        cancel    # idempotent + cheap → re-run
    world:       adopt     # ADR-0011: child re-executes its world ops itself
    side_effect: adopt     # avoid double-write
    external:    adopt     # highest duplicate-effect risk
    llm:         adopt     # cost optimization
```

Validation matches `_build_skill_resume_config` precedent: invalid
top-level value → fall back to default with `_log.warning`; invalid
purity key or action → log + skip.

**No `per_skill` / `per_goal_pattern`** — plans have no stable
identifier, regex over freeform goal text is fragile.

`resume_from_step` is operator-only via slash command (`/plan resume
<plan_id> --from <step_id>`), not yaml-settable.

#### Skill-side declaration

Skills declare their resume purity in frontmatter:

```yaml
# skill.md frontmatter
resume_purity: side_effect   # one of pure | world | side_effect | external | llm
```

Default `side_effect` (= safest assumption when undeclared). Read by
the coordinator as the input to `child_purity:` policy lookup.

P7-clean: the field is a **generic enum**; no skill name or artifact
name leaks into OS code.

#### Coordinator API

```python
class PlanResumeCoordinator:
    def discover_and_decide(*, plan_registry, skill_registry, state_log,
                            policy) -> list[PlanResumeDecision]: ...
    def decide_for_plan(plan_resume_plan, skill_registry, policy) -> PlanResumeDecision: ...
    async def apply_decisions(decisions, *, plan_registry, skill_registry,
                              notify_outbox=None) -> list[PlanResumeDecision]: ...

@dataclass(frozen=True)
class PlanResumeDecision:
    plan: PlanResumePlan
    action: PlanResumeAction
    pending_step_ids: list[str]
    child_actions: dict[str, PlanResumeChildAction]  # child_run_id → adopt|cancel
```

`apply_decisions` performs **destructive side effects only**:
- `action="discard"`: cancel every spawned child via
  `skill_registry.complete(status="discarded")`, drop interventions,
  emit `plan_aborted`, surface outbox notice.
- `action="retry_pending"`: cancel children with
  `child_actions[run_id]=="cancel"`; **leave** adopted children for
  the existing skill auto-resume to pick up.

The launchable subset (= `action in {"resume", "retry_pending"}`) is
returned for `ChatSession._spawn_resumed_plan` to handle.

#### Ordering with skill auto-resume

The plan coordinator must run **before** `_auto_resume_active_skills`
or pass a `claimed_run_ids: set[str]` filter to it. Otherwise adopted
children get auto-resumed twice. Recommended: run plan coordinator
first (= symmetric with Phase 1 cleanup ordering).

### 3.4 PlanRuntime

#### API contract

```python
class PlanRuntime:
    def __init__(
        self,
        plan: Plan,
        *,
        host: RouterLoopHost,
        chain_id: str,
        plan_id: str | None = None,
        budget: BudgetTracker | None = None,
        router_model: str = "light",
        resume_plan: PlanResumePlan | None = None,
    ) -> None: ...

    async def run(self) -> PlanExecutionResult: ...
```

Naming: `PlanResumePlan` (= analyzer output dataclass) is **distinct**
from skill-side `ResumePlan`. Both runtimes accept their own resume_plan
type; no shared base class.

#### Step classification on resume

`_classify_step(step) -> Literal["memo", "child_in_flight", "execute"]`
is a **pure function over `(step.id, resume_plan)`**:

| Condition | Decision |
|---|---|
| `step.id ∈ resume_plan.committed_step_ids` | `memo` |
| `step.id == in_flight_child_step_id` | `child_in_flight` |
| `step.id ∈ pending_step_ids` | `execute` |
| `resume_plan is None` | `execute` (= fresh run) |

#### Memoization granularity

**Step-level only.** A step's full output text is the memo unit. The
internal child `RouterLoop` is **re-run from scratch** for incomplete
steps (= max 3 iterations × light-model LLM calls; bounded cost).

This rejects sub-loop resume (= would double the recovery substrate
without proportionate user-visible value). Plan steps that spawn child
skills get the expensive resume via the existing skill_resume
infrastructure; PlanRuntime's job is adoption / cancellation, not
sub-loop op-level memo.

#### ADR-0013 finally pattern

```python
finally:
    exc_type = sys.exc_info()[0]
    if exc_type is None or _is_workflow_abort(exc_type):
        await host.record_plan_completed(plan_id=plan_id)
        host.delete_plan_decomposition(plan_id)  # P5 cleanup
    else:
        host.events.emit(
            "plan_run_interrupted",
            plan_id=plan_id,
            exc_type=exc_type.__name__,
        )
        # active_plan_ids preserved; decomposition artifact preserved
```

Same exception classification as `OSRuntime.run` (= ADR-0013).
`kill -9` bypasses `finally` entirely; the WAL-only `plan_started`
record handles that path.

#### ChatSession integration

```python
# ChatSession.startup() — mirrors _spawn_resumed_skill pattern
async def _spawn_resumed_plan(prp: PlanResumeDecision):
    plan = load_plan_decomposition(prp.plan.plan_id)
    runtime = PlanRuntime(
        plan, host=router_loop_host, chain_id=prp.plan.chain_id,
        plan_id=prp.plan.plan_id, budget=budget_tracker,
        router_model=router_model, resume_plan=prp.plan,
    )
    task = asyncio.create_task(runtime.run())
    self._spawned_resumed_plans[prp.plan.plan_id] = task
```

A new `running_plans: dict[plan_id, asyncio.Task]` dict on `ChatSession`
mirrors the existing `running_skills` dict. Enables `/plan discard
<plan_id>` slash (= future, not Phase 2 v1).

### 3.5 Decomposition artifact

#### Why a workspace artifact

LLM-emitted decomposition is **non-deterministic**: re-calling the
planner LLM on resume yields a different plan, breaking step-result
memoization (= new `step_id`s don't match recorded keys). The Plan
dataclass must be persisted as a workspace artifact (P5 SSoT) and read
verbatim on resume.

#### Hybrid persistence

```
.reyn/agents/<agent_name>/state/plans/<plan_id>/decomposition.json
```

If a workspace exists for plans (= future Phase 3 work to add per-plan
workspaces), the artifact lives there. Phase 2 v1 uses the snapshot-
adjacent path above.

Snapshot field semantics:
- `decomposition_artifact_path` set → canonical source on resume
- `steps_serialized` populated → fallback when artifact unreadable

Format:

```json
{
  "plan_id": "ab12cd34",
  "schema_version": 1,
  "goal": "...",
  "steps": [
    {"id": "s1", "description": "...", "tools": ["read_file"], "depends_on": []}
  ]
}
```

#### Lifecycle ordering

1. `dispatch_plan_tool` after `parse_and_validate_plan` succeeds:
   atomic write (= tmp + rename) of decomposition artifact.
2. `dispatch_plan_tool` constructs `PlanRuntime` + awaits `run()`.
3. PlanRuntime's finally clause: on completion/abort, delete the
   artifact; on crash, leave it for restart cleanup.
4. `AgentRegistry.restore_all` cleanup: orphan artifacts deleted with
   the snapshot.

Order matters: artifact write **before** `record_plan_started` ensures
that any plan in `active_plan_ids` has a discoverable decomposition.

#### Corruption fallback

If `decomposition_artifact_path` is unreadable on resume, the
coordinator forces `action="discard"` regardless of policy. A distinct
outbox reason ("plan decomposition artifact missing or corrupt; please
retry") clarifies the failure mode for the user.

### 3.6 ChainManager / cross-agent notify (preserved divergence)

ADR-0022 documented: plan-mode does **not** register its own
`chain_id`; the user is the implicit waiter via outbox; R-D14
`notify_chain_discarded` is not invoked for plan abort.

Phase 2 preserves this. Children spawned by plan steps already have
their own `chain_id` (= allocated in `run_skill_awaitable`); when the
plan coordinator cancels a child via
`skill_registry.complete(status="discarded")`, the existing R-D14 path
fires for any peer agent waiting on the **child**. This is correct.

The integration is **implicit and free** — no new ADR-0018 surface
needed in plan-mode.

### 3.7 ADR-0012 carryover (no prompt at restart)

ADR-0012 retired skill bulk resume prompts because (i) ADR-0011's
world-purity invalidation removed the structural ambiguity prompts
were trying to rescue, (ii) binary "continue all / abort all" had no
decision payoff over default `retry` + slash discard, (iii) the skill
author's `description` bore inappropriate UX weight.

Plan-mode applies the same logic, with a stronger frequency argument:

- One in-flight plan per chat session at most (= much lower bulk
  pressure than skills).
- Per-purity policy resolves adopt-vs-cancel offline; no per-restart
  user input adds value.
- Children's own `skill_resume` policy already handles their internal
  ambiguity at the appropriate layer.

→ **No `prompt` value in `plan_resume.default`.** `prompt_required`
remains in the type system as extensibility; no Phase 2 path produces
it. `resume_from_step` slash command provides the surgical operator
escape hatch.

## Migration path (Phase 1 → Phase 2)

7-step ordering, each step landable independently:

1. **Decomposition artifact helpers** (= write/read/delete on
   RouterLoopHost). Standalone with tests, no behavior change.
2. **PlanSnapshot dataclass** + persistence helpers + `PLAN_SNAPSHOT_VERSION`.
3. **PlanRegistry** as the WAL-coordination wrapper around per-plan
   snapshot lifecycle (= mirror `SkillRegistry` shape).
4. **Promote plan_step_*** events from events-log to WAL (= add to
   `WAL_EVENT_KINDS`, wire `record_plan_step_started/completed/failed`
   on SnapshotJournal). Phase 1's existing emits become WAL appends.
5. **PlanRuntime** as a thin wrapper around the existing
   `execute_plan` body. First cut: `PlanRuntime.run` literally calls
   the existing free function. Phase 1 tests survive unchanged.
6. **Migrate `dispatch_plan_tool`** to construct `PlanRuntime`. Fresh-
   run path only; resume_plan stays `None`. Add decomposition artifact
   write before runtime construction.
7. **Land `_classify_step` + memo-replay logic** + `PlanResumeCoordinator`
   + `ChatSession._spawn_resumed_plan`. Resume path becomes live.

After step 7, switch `restore_all`'s default policy from "discard" to
coordinator-driven. This is the user-visible Phase 2 cutover.

`tests/test_plan_lifecycle_crash.py` (10 tests from Phase 1) survives
every step. New tests at each step:
- step 1: artifact write/read/delete round-trip
- step 2-3: PlanSnapshot save/load + apply_events
- step 4: WAL event ordering after restart
- step 5: PlanRuntime fresh-run parity with execute_plan
- step 7: full resume e2e (= LLMReplay fixture for committed-step memo)

## Consequences

### Positive

- **Step results preserved across crash.** Long plans no longer re-pay
  LLM tokens on resume.
- **Adopt vs cancel coordinated with skill_resume.** Spawned children
  are handled per declared purity, no orphans.
- **P5-clean**: decomposition is canonical workspace artifact.
- **No new resume primitive layer.** PlanRuntime reuses every shape
  that worked for skills (atomic save, schema versioning, analyzer/
  coordinator, ADR-0013 finally).
- **Schema-compatible with Phase 1.** No `AgentSnapshot` version bump;
  Phase 1 → Phase 2 upgrade requires no `--reset`.

### Negative

- **Substrate growth.** New module `src/reyn/plan/` (= snapshot,
  registry, runtime, resume_analyzer, resume_coordinator) parallel to
  `src/reyn/skill/`. Roughly doubles the recovery code surface.
- **Skill author burden.** `resume_purity:` field added to skill.md
  frontmatter (= optional with `side_effect` default; no breakage for
  unaware authors).
- **WAL volume.** Promoting `plan_step_*` to WAL increases events per
  plan (= 3 events per step × 2-7 steps). Truncation watermark
  ensures bounded retention.
- **Sub-loop work re-paid.** A step that did substantial pure-LLM work
  without spawning a skill re-runs from scratch on resume. Acceptable
  per §3.4.

### Open issues / explicit non-goals

- **Nested plans** (= a skill spawning a plan). Out of scope; chat-
  router is the only `plan` tool surface today. If skills gain plan-
  mode access, `child_purity` would need a `plan` row.
- **Multi-process plan resume.** Out of scope (= ADR-0001 invariant).
- **Cross-agent plan resume.** Out of scope. Plans run within one
  agent's chat turn.
- **Plan-spawned plans** (= recursion bound). Currently impossible
  (`_PlanStepHost` excludes `plan` from step tool catalog,
  commit `7d0d6a2`). If lifted, `plan_depth` counter required.
- **Adopt timeout.** A child that hangs on resume blocks plan
  finalization. Phase 2 v1 trusts the child; Phase 2.1 may add
  `plan_resume.adopt_timeout_seconds`.
- **Per-step retry granularity** (= "retry just step 3 again" after
  step-3-only failure). Phase 2 does whole-plan retry with per-step
  memo; finer granularity deferred until dogfood demands it.
- **`/plan discard <plan_id>` slash command.** Useful for the
  cancellation case but not strictly required for Phase 2 correctness.
  Tracked as follow-up.
- **Step result size cap.** `step_results: dict[str, str]` could grow
  pathologically (= multi-page web scrape). Phase 2 v1: bound at
  write time (= 32KB truncation with "[truncated]" suffix). Spill-to-
  side-files pattern (R-D10 mirror) deferred.

## Cross-references

- **ADR-0001** (state model — WAL + snapshot): plan-mode now also
  participates. WAL truncation floor extended (§3.1).
- **ADR-0002** (forward-replay resume): conceptual peer. PlanRuntime
  fast-forwards at step granularity, OSRuntime at phase granularity.
- **ADR-0003** (op purity): `child_purity:` table in `plan_resume:`
  reuses the same purity tiers.
- **ADR-0006** (schema version refuse): `PLAN_SNAPSHOT_VERSION = 1`
  follows the same refuse + `--reset` policy.
- **ADR-0009** (visit count decrement): not applicable; plans don't
  re-enter steps.
- **ADR-0011** (world-purity cross-run invalidation): inherited via
  child skills; plan-step level reuses the classification.
- **ADR-0012** (auto-resume + retry default): mirrored exactly. Default
  is `retry_pending_steps`; no prompt at restart.
- **ADR-0013** (runtime crash lifecycle): exception-aware finally
  pattern in PlanRuntime mirrors OSRuntime.
- **ADR-0017** (parent_run_id nested skill path): `parent_skill_run_id`
  on PlanSnapshot is the analog field.
- **ADR-0018** (cross-agent discard notify): preserved divergence —
  plan abort notifies user via outbox; R-D14 path fires for cancelled
  children automatically.
- **ADR-0022** (Phase 1 fail-safe): direct predecessor. Phase 2 is
  additive on top of Phase 1's substrate; Phase 1 → Phase 2 is
  non-breaking.

## Future work

- Phase 3: per-plan workspace allocation (= drop `steps_serialized`
  inline fallback, all decompositions live in workspace).
- Phase 3: `/plan list` / `/plan discard` / `/plan resume --from`
  slash commands.
- Phase 3: cross-agent plan coordination (= if multi-agent topology
  grows to support plan delegation).
- Phase 3: sub-loop op-level memoization (= if dogfood shows hot
  expensive-LLM-step paths).
- Audit hash chain (= ADR-0001 future work) extension to plan WAL
  events.
