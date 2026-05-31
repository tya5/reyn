# Plan mode

How Reyn decomposes a complex chat query into independent sub-tasks,
runs them in narrow LLM calls, and stitches the answers together —
with crash resilience so long plans survive process restarts.

## What plan mode is

For complex queries (= multi-source synthesis, "explain X with code
references", compare-and-contrast), the chat router can call the
`plan` tool to produce a structured decomposition:

```
user query → planner LLM
                ↓
              [plan: 2-7 steps with tools + dependencies]
                ↓
              executor: each step runs in a narrow LLM call
                ↓
              terminal step's text → user reply
```

Each plan step gets a small, focused system prompt and a subset of
the parent's tool catalog. This avoids the per-call context bloat
that comes from carrying the full router prompt + 14 tools through
every sub-task.

Plan mode is **opt-in per query** — the router LLM picks `plan` when
it sees a query that warrants decomposition. Simple queries
(= "hello", single tool call) bypass it entirely.

## Async dispatch

`plan` is registered as an async tool. When the router LLM calls it,
the chat turn does not block:

1. `dispatch_plan_tool` validates the plan, allocates a `plan_id` +
   per-plan `chain_id`, writes the decomposition artifact, and spawns
   a `PlanRuntime` task.
2. The router loop sees an async tool result and exits — the chat
   turn ends.
3. The plan task runs in the background. Per-step status messages
   land in the outbox so the user sees progress.
4. The terminal aggregator step's text is emitted to the user as a
   regular agent message (`kind="agent"`, `meta.plan_id` for
   identification).

This matches the human dispatch model: quick replies first, long
work continues in the background. The user can issue new chat
messages while a plan is in flight; multiple plans can run
concurrently.

## What's preserved across crashes

| State | Where | Survives crash |
|---|---|---|
| Decomposition (plan shape) | `agents/<name>/state/plans/<plan_id>/decomposition.json` | yes |
| Per-plan progress (steps completed, results) | `agents/<name>/state/plans/<plan_id>.snapshot.json` | yes |
| Step output (≤32 KB) | inline on the snapshot above | yes |
| Step output (>32 KB) | `agents/<name>/state/plans/<plan_id>/step_results/<step_id>.txt` | yes |
| Plan lifecycle events | `.reyn/state/wal.jsonl` (`plan_started` / `plan_step_*` / `plan_completed`) | yes |
| Active asyncio.Task | in-memory only | no — auto-resumes on next startup |

Step outputs > 32 KB spill to a per-plan workspace file (ADR-0024)
so the snapshot stays small and outputs are preserved verbatim
(= no truncation, no data loss). Read access goes via
`get_step_result(snap, agent_state_dir, step_id)` which transparently
resolves inline-vs-spilled.

Sub-loop LLM calls within each step are also recorded
(ADR-0025) so a
crash mid-step doesn't re-pay the LLM cost on resume. The sub-loop's
`call_llm_tools` invocations are keyed by `args_hash`; on resume the
recorded results replay before the LLM is invoked, only the crashed
turn pays fresh.

When `reyn chat` next starts, `AgentRegistry.restore_all`:

1. Replays the WAL onto each agent's snapshot.
2. For every plan in `active_plan_ids`, calls
   `_recover_plans_for_agent`:
   - Loads the per-plan snapshot.
   - Reads the decomposition artifact (P5 SSoT — the LLM's plan, not
     re-derived because re-decomposition is non-deterministic).
   - Runs the resume coordinator (= analyzer + policy) to classify
     each step as `pending` / `completed_with_result` / `failed` /
     `interrupted_with_child`.
   - Spawns a `PlanRuntime` task with `resume_plan` set so completed
     steps memo-replay (= no LLM cost), only pending ones re-execute.

Long plans no longer re-pay LLM tokens on resume — the recorded
`step_results` are reused.

## Multi-plan and ordering

Multiple plans can be in flight at the same time. Each has its own
`plan_id` + `chain_id` + decomposition directory; they're
independent. Outbox messages land in **completion order**, not
arrival order — a 30-second plan that finishes before a 5-minute
one shows up first, with `meta.plan_id` distinguishing them.

The WAL truncation floor includes every active plan's
`last_step_applied_seq`, so step events the resume analyzer needs
aren't dropped while a plan is still running.

## Step execution behavior

### Plan summary before execution

Before the first step runs, Reyn emits a status message listing all
planned steps so the user sees the full shape upfront:

```
以下の計画で実行します:
1. <step 1 description>
2. <step 2 description>
…
```

### Step progress status

Each step emits a progress status:

```
plan step {n}/{total}: <step description>
```

The description is taken from the step's `description` field (truncated
to 60 characters) rather than the internal step ID, so the status is
human-readable.

### Automatic retry on transient failure

When a step fails, Reyn automatically retries up to `retry_limit`
times (default 3) before escalating. Each retry emits a status
message:

```
リトライ {attempt}/{retry_limit}: <step description>
```

When the retry budget is exhausted, Reyn invokes the
`handle_limit_exceeded` intervention — the user sees a prompt asking
whether to extend the retry budget. On approval the budget is extended
by the base limit and execution continues; on refusal the step is
recorded as failed.

### Failed steps and plan continuation

A step failure does **not** abort the whole plan. Reyn records the
failure in `step_failures`, emits a `plan_step_failed` event, notifies
the user:

```
plan step {n}/{total}: <description> → 失敗 (<error summary>)
```

…and continues with remaining steps. Steps that declare a dependency
on a failed step receive a synthetic result noting the failure
(`"(FAILED: ...)"`) so dependent steps can handle it gracefully.

### Step execution config

```yaml
# reyn.yaml
plan:
  step_max_iterations: 5   # max RouterLoop iterations per step (default: 5)
  retry_limit: 3           # max auto-retries per step on transient failure (default: 3)
```

`step_max_iterations` caps how many LLM sub-loop turns a single step
may consume. `retry_limit` is a cost protection upper bound (analogous
to token budget limits).

## Resume policy

`reyn.yaml` configures coordinator behavior:

```yaml
plan_resume:
  default: retry_pending       # one of: retry_pending | discard
  child_purity:                # for plan steps that spawned a child skill
    pure:        cancel        # idempotent + cheap → re-run
    world:       adopt         # child handles its own resume
    side_effect: adopt
    external:    adopt
    llm:         adopt
```

- `retry_pending` (default) — memo committed steps, re-execute the
  rest.
- `discard` — abort the plan, cancel children flagged `cancel`,
  surface an outbox notice asking the user to re-issue.

Plans whose decomposition artifact is missing or corrupt are
auto-discarded with a descriptive outbox notice — Reyn never
re-decomposes via the planner LLM (that would shuffle step IDs and
break memoization).

## Operator commands

```
/plan list                                — show active plans (running + pending resume)
/plan discard <plan_id>                   — abort + clean up state
/plan resume <plan_id> --from <step_id>   — re-run from a specific step
```

`/plan discard` cancels the asyncio.Task, records `plan_aborted` to
the WAL, removes the decomposition artifact + snapshot, and notifies
any peer agent waiting on the plan's chain (R-D14).

`/plan resume --from` is the surgical escape hatch (= ADR-0023 §3.7)
for the case where a step recorded a result the operator wants to
redo (e.g. the LLM produced something wrong, or world state shifted
in a way the recorded result no longer reflects). The handler:

1. Cancels any in-flight task for the plan.
2. Loads the decomposition artifact for topological step order.
3. Clears `step_results` / `step_failures` / `spawned_skill_run_ids`
   from `<step_id>` onward; preserves earlier steps.
4. Rebuilds a `resume_plan` and re-launches via the standard auto-
   resume path — earlier steps memo-replay (no LLM cost), the rest
   re-execute.

The sub-command rejects unknown plan IDs, missing decomposition
artifacts (= directs to `/plan discard`), and step IDs not in the
plan (= lists valid step IDs).

## Crash classification

Mirrors [skill resume](../skills/skill-resume.md) — exception-aware finally
clause in `PlanRuntime.run`:

| Exit | Outcome |
|---|---|
| Normal return | `plan_completed` recorded; artifact deleted; user gets terminal text |
| `WorkflowAbortedError` | Treated as clean abort; artifact deleted |
| Generic `Exception` / `KeyboardInterrupt` | `plan_run_interrupted` event; artifact preserved; restart auto-resumes |
| `kill -9` | `finally` skipped; artifact preserved; restart auto-resumes |

The artifact preservation invariant is what makes resume work — if
the artifact is gone, the coordinator can't reconstruct the plan
shape and falls back to discard.

## Cross-references

- [skill resume](../skills/skill-resume.md) — sibling design; plans reuse the
  same WAL + snapshot + analyzer + coordinator patterns
- [permission model](../runtime/permission-model.md) — plan steps run with
  per-step narrowed tool catalogs
- [events](../runtime/events.md) — `plan_*` and `plan_step_*` audit trail
- ADR-0022 (Phase 1 fail-safe), ADR-0023 (Phase 2 forward replay +
  Phase 2.1 async dispatch)
- FP-0028 (step progress UX), FP-0029 (step iteration budget),
  FP-0030 (step result quality), FP-0031 (retry + user confirmation)
