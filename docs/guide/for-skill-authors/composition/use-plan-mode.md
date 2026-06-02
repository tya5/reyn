---
type: how-to
topic: composition
audience: [human]
applies_to: [reyn chat]
---

# Use plan mode for multi-step tasks

**Goal:** When a user asks for a multi-step task in chat — "research X, then summarize, then write a doc" — plan mode lets the agent decompose the request into ordered steps, dispatch them asynchronously in the background, and stream progress back as each step completes. Completed state persists across restarts; the operator can inspect or redirect mid-execution.

## When to use

Use plan mode when:

- The user's request decomposes naturally into three or more ordered steps.
- Some steps are slow — web fetch, multi-file analysis, sub-skill chains.
- The session might be interrupted and crash recovery matters.
- The operator wants to inspect or redirect execution mid-plan.

Do not expect plan mode for:

- Single-shot prompts or fast direct LLM responses.
- Two-step tasks where a simple `run_skill` composition suffices — see [Compose skills with `run_skill`](compose-skills-with-run-skill.md).

## How plan mode is triggered

Plan mode is invoked when the router LLM picks the `plan` tool from its catalogue. It is not a slash command — the agent decides based on query complexity. Common phrasing that triggers it:

- "Step by step, ..."
- "First X, then Y, then Z"
- Long open-ended goals: "write a research summary on AI agents in healthcare"

**Example exchange:**

User message:

```
Research the top 3 open-source agent frameworks, compare their design
philosophies, then write a 500-word summary I can share with the team.
```

The router LLM calls `plan` and produces a decomposition:

```
step_1: search — collect overviews of LangChain, AutoGen, CrewAI
step_2: analyze — compare design philosophy across the three
step_3: write — produce the 500-word summary (depends on step_1, step_2)
```

The chat turn ends immediately (async dispatch — the turn does not block). As steps complete, the operator sees status messages:

```
[plan abc1] step_1 complete
[plan abc1] step_2 complete
[plan abc1] step_3 complete — result delivered
```

The terminal step's output arrives as a regular agent message.

## Inspect what is happening

Three slash commands are available while a plan is in flight. Syntax is verified against [`reference/cli/chat.md`](../../../reference/cli/chat.md).

### `/plan list`

```
/plan list
```

Shows all active plan runs — in-flight tasks and plans pending auto-resume after a crash. Use this first to get a `plan_id` and the current active `step_id`.

### `/plan discard <plan_id>`

```
/plan discard abc1
```

Aborts the plan, cancels its asyncio task, cleans up state (decomposition artifact + snapshot), and notifies any peer agent waiting on the plan's chain. Use when the plan is going off-rails and you want a clean slate.

### `/plan resume <plan_id> --from <step_id>`

```
/plan resume abc1 --from step_2
```

Surgical escape hatch. Clears recorded results from `step_id` onward, preserves earlier steps, and re-launches. Steps before the target memo-replay at no LLM cost; the target step and everything after re-execute fresh. Use when a specific step produced wrong output and you want to redo it without restarting the whole plan.

The command rejects:
- Unknown plan IDs.
- Missing decomposition artifacts (directs to `/plan discard` instead).
- Step IDs not in the plan (lists valid IDs).

## State persistence — what survives a crash

If the `reyn chat` process dies mid-plan, restart and the agent automatically resumes. The table below matches what is preserved:

| State | Survives crash |
|---|---|
| Plan decomposition (step shape) | yes |
| Per-step progress and results | yes |
| Step output ≤ 32 KB | yes — stored inline in the snapshot |
| Step output > 32 KB | yes — spilled to a per-plan workspace file |
| Active asyncio.Task | no — recreated on restart |

On next startup, `AgentRegistry.restore_all` replays the WAL, classifies each step as completed or pending, and spawns a `PlanRuntime` task. Completed steps memo-replay (no LLM cost); only pending steps re-execute.

The decomposition artifact is never re-derived via the planner LLM — re-decomposition is non-deterministic and would shuffle step IDs, breaking memoization. If the artifact is missing or corrupt, the coordinator auto-discards and surfaces an outbox notice.

For the conceptual model behind this behavior, see [concepts/multi-agent/plan-mode.md](../../../concepts/multi-agent/plan-mode.md).

## Operator intervention recipes

**A step output is wrong.** Use `/plan resume <plan_id> --from <step_id>` after fixing the relevant skill or prompt. Earlier steps replay from memo; only the target and downstream steps re-run.

**The plan is going off-rails.** Use `/plan discard <plan_id>` to abort cleanly, then re-issue the original request with a refined prompt.

**Run two multi-step tasks in parallel.** Start a second open-ended request while a plan is in-flight. Each plan gets its own `plan_id` and `chain_id`; they are independent. Outbox messages arrive in completion order, not submission order — a shorter plan finishing first shows up first. The `meta.plan_id` field distinguishes replies.

**Trace what happened.** After a plan completes (or fails), inspect the event log:

```bash
reyn events .reyn/agents/<name>/events.jsonl --filter plan_step_completed
```

Each `plan_step_completed` event carries the `step_id`, duration, and whether the result was memo-replayed or freshly computed.

## Common pitfalls

**"Why isn't my plan auto-resuming?"** Check `reyn.yaml`. The `plan_resume.default` key controls behavior: `retry_pending` (default) resumes pending steps; `discard` aborts and surfaces a notice asking the user to re-issue. If the key is set to `discard`, auto-resume is intentionally disabled.

**"Step output looks truncated."** Inline output is capped at 32 KB; larger results spill to a file in the per-plan workspace directory. This is not data loss — the full output is available in `agents/<name>/state/plans/<plan_id>/step_results/<step_id>.txt`. The `get_step_result` accessor resolves inline vs. spilled transparently.

**"I see `plan_aborted` events on restart."** A plan that was in-flight when the process died surfaces as an outbox notice on the next startup if `plan_resume.default` is `discard`. With the default `retry_pending` policy, you see `plan_resumed` instead.

**"My plan is too granular / too coarse."** Ask the agent to re-plan: "That's too many steps — combine the research and analysis into one." Or seed a step-count hint: "Do this in exactly two steps." The planner LLM responds to natural-language decomposition hints.

## See also

- [concepts/multi-agent/plan-mode.md](../../../concepts/multi-agent/plan-mode.md) — conceptual model: async dispatch, crash classification, resume policy, multi-plan ordering.
- [reference/cli/chat.md](../../../reference/cli/chat.md) — full slash command reference, including the `/plan` family.
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md) — when a single sub-skill suffices instead of a multi-step plan.
