# Async skill execution

How the chat router fires off long-running skills without freezing the
prompt — and how completion narration arrives back in the conversation
without blocking the LLM context.

## Why async

A skill run can take seconds to minutes (skill_builder, mcp_search, an
eval round, a real workload skill). Until FP-0012, `invoke_skill`
**blocked** the chat session's main loop:

```
Session.run() main loop — sequential, single consumer
─────────────────────────────────────────────────────
kind, payload = await _consume_inbox()         ← blocked here during skill run
await _handle_user_message()
  └─ RouterLoop.run()
       └─ await invoke_skill tool
            └─ await _run_skill_awaitable()
                 └─ await agent.run()           ← minutes pass here
                                                   user types 3 messages
                                                   → all queue in inbox
                                                   → none are processed
```

Anything the user typed during those minutes silently queued in the
inbox. No acknowledgment, no progress feedback, no way to ask a quick
clarifying question while the long task ran.

Chat-mode `invoke_skill` is now non-blocking. Plan-mode keeps blocking
semantics on purpose (= sequential step execution needs the nested
skill's result inline to feed the next step's LLM).

## How chat-mode action dispatch works

Since FP-0034 Phase 6 (2026-05-16), the LLM-visible surface is
`invoke_action(action_name="skill__<name>", args={"input": ...})`.
`universal_dispatch.py` routes the wrapper call to the same internal
`invoke_skill` handler — the spawn-ack mechanism is unchanged.
The legacy `invoke_skill(name, input)` direct form is no longer
the production surface (kept internally for plan-mode; see below).

```
User: "skill_builder で string_length を作って"
  └─ RouterLoop: invoke_action(action_name="skill__skill_builder",
                               args={"input": {...}})
       └─ universal_dispatch → invoke_skill handler
       └─ _handle: spawn_skill_fn → ChatSession._spawn_skill_for_router
            └─ asyncio.create_task(_run_one_skill(...))
            └─ returns IMMEDIATELY:
                 {status: "spawned", run_id, chain_id, skill, note}
       └─ Router LLM sees the spawn ack, generates 1-sentence reply
  └─ Router LLM → user: "Started skill_builder — I'll let you know when
                         it finishes. /tasks shows progress."
  └─ Session loop: free — processes the next inbox message immediately

User: "ちなみに recall の設定どうなってた？"
  └─ RouterLoop: recall(...) → router LLM answers inline
  └─ User sees the answer — skill_builder is still running in background

[2 minutes later] skill_builder completes
  └─ _run_one_skill → _enqueue_skill_completed → _put_inbox("skill_completed", {...})
  └─ Session.run() picks up the inbox kind and calls
     _handle_skill_completed(payload):
        ├─ append a user-role ChatMessage to history:
        │     "[task_completed] chain_id=... run_id=...
        │      skill: skill_builder  status: finished
        │      result: {skill_name: string_length, path: ...}
        │      Please summarize for the user in 1-2 sentences."
        └─ run one router LLM turn (LLM has full thread context)
  └─ Router LLM → user: "skill_builder が完了しました。
                          reyn/project/string_length/ に作成されました。"
```

The two LLM moments are:

1. **Spawn ack** — when invoke_skill returns `{status: "spawned", ...}`,
   the router LLM produces a 1-sentence acknowledgment and a pointer
   at `/tasks` for progress inspection. It must not call invoke_skill
   again for the same request and must not ask follow-up questions
   about the in-flight task.
2. **Completion narration** — when a `[task_completed]` user-role
   message arrives in the conversation thread, the LLM extracts the
   user-relevant fields from `result` and narrates in 1–2 sentences.

The router system prompt's `Behaviour` section pins both rules.

## Why a user-role message instead of a fresh tool_result?

Both OpenAI Chat Completions and Anthropic Messages API enforce a
strict pairing: a `tool_result` / `role: "tool"` message must be
preceded by an `assistant` message containing the matching `tool_use` /
`tool_calls` block. By the time the async task finishes, the original
`invoke_skill` `tool_use` already has its paired `tool_result` (= the
spawn ack). There is no open `tool_use` to pair a second result with —
sending one returns a 400 from the API.

So completion is delivered as a synthesized `user`-role message
(`meta.source = "skill_completion"`) inserted into the existing
conversation thread. The router LLM has full thread context (= the
original spawn ack, any intermediate exchanges with the user, and now
the completion), so `chain_id` and `run_id` give it the correlation
back to the specific invocation. Multiple concurrent skills each carry
their own chain_id; the LLM distinguishes which task finished.

## Anti-optimism on errors

Skill terminal status can be `finished`, `loop_limit_exceeded`, or
`error`. The router system prompt's narration rules require:

- on `finished`: confirm completion, optionally hint at the next step.
- on `loop_limit_exceeded`: say the skill ran out of phase budget and
  suggest re-running with a higher `safety.loop.max_phase_visits`.
- on `error` / any non-`finished` status / when `result.error` is
  present: the reply MUST surface the specific error verbatim. It
  must NOT be narrated as success. Quote the error in user-friendly
  form (translated to `output_language` if set, but the failure
  signal stays explicit) and suggest the most likely fix.

The 2026-05-10 G4 spike observed the strong (gemini-2.5-flash) tier
narrating success even when status was `error` and a `data.error`
field was populated. The MUST-surface rule landed alongside FP-0011
Component B's strengthening to address that flash-tier optimism bias.

## A2A / MCP bypass path

`reyn mcp serve` and the FastAPI A2A endpoint (`reyn web`) both reach
the agent through `mcp_server.send_to_agent_impl`, which drives
`ChatSession._handle_user_message` **inline** rather than going
through `session.run()`. Under the MCP SDK's stdio transport an
`asyncio.create_task`-spawned `session.run()` coroutine is starved
while the request handler awaits — the LLM call never makes
progress and the handler times out empty. So the bypass keeps
everything on the single event-loop task the SDK is scheduling.

The trade-off: nothing on the bypass path consumes the
`skill_completed` inbox kind that FP-0012 introduced. Without
explicit draining, a non-blocking `invoke_skill` returns only the
spawn ack — the completion narration never fires for A2A-driven
agents.

`send_to_agent_impl` closes that gap in three steps after
`_handle_user_message` returns, all within the remaining timeout
budget:

1. `await asyncio.gather(*running_plans)` — plan-mode async tasks
   (= ADR-0023 §2.1.1) finish and append their terminal text to
   history.
2. `await asyncio.gather(*running_skills)` — spawned skills run to
   terminal status and enqueue `skill_completed` via
   `_enqueue_skill_completed`.
3. `session.drain_skill_completed_inbox(deadline_monotonic=...)` —
   pops `skill_completed` items non-blockingly, records the WAL
   consume entry, and dispatches each one to
   `_handle_skill_completed` (which runs the router LLM for
   narration). Non-`skill_completed` kinds are re-queued FIFO so the
   next consumer sees them.

If the deadline fires mid-drain, `partial=True` is returned and the
remaining items stay on the inbox for the next call to pick up.

## Plan-mode keeps blocking semantics

Plan-mode RouterLoops bind only `run_skill_fn` (= the legacy blocking
path); `spawn_skill_fn` is left None. So when a plan step's LLM calls
`invoke_skill`, it blocks until completion and the result feeds the
next step inline. This is intentional — plan steps are sequential and
the next step's prompt frequently includes the previous step's
outcome. Spawn-and-return semantics would force the planner to
build its own completion-tracking layer and is not worth the
complexity.

The split is wired in `RouterLoop._build_router_caller_state`:

```python
_spawn_skill_bound = None
if hasattr(self.host, "spawn_skill") and callable(...):
    _spawn_skill_bound = ...

return RouterCallerState(
    run_skill_fn=_run_skill_bound,        # always present
    spawn_skill_fn=_spawn_skill_bound,    # chat-mode only
    ...
)
```

Plan-mode's `_PlanStepHost` does not implement `spawn_skill`, so the
hasattr check fails and the binding stays None.

## Slash commands

`/tasks` is the unified entry point spanning skill runs and plan
tasks:

```
/tasks                          → list all running tasks (skills + plans)
/tasks list                     → same as /tasks
/tasks status <run_id_prefix>   → current phase + elapsed time + chain_id
/tasks kill <run_id_prefix>     → cancel a specific task
```

Legacy commands continue to work as aliases:

- `/skill list` / `/skill discard <run_id>` — skills only (PR-resume-ux U2)
- `/plan list` / `/plan discard <plan_id>` — plans only (ADR-0023)

## What's preserved across crashes

| State | Where | Survives crash |
|---|---|---|
| inbox queue (incl. `skill_completed`) | `agents/<name>/state/inbox.snapshot.json` | yes (PR21) |
| spawned task (asyncio.Task in memory) | session-only | **no** — running tasks die with the process |
| `running_skills_*` dicts | session-only | no |
| Skill state mid-execution | per-skill snapshots + WAL | yes (PR-resume-auto / ADR-0023) |

A skill that was spawned but did not finish before a crash is
resumable through the standard skill-resume infrastructure (= per-skill
snapshot + WAL replay) — it does NOT depend on the chat session's
`running_skills` dict or the `skill_completed` inbox. After restore,
the auto-resume coordinator re-launches active skills; on completion
they enqueue `skill_completed` against the restored inbox just like
a fresh run.

## See also

- [Concepts: plan-mode](plan-mode.md) — sequential step execution
  (= explicitly blocking, contrast with chat-mode async)
- [Concepts: skill-resume](skill-resume.md) — crash recovery for
  in-flight skill state
- [Reference: chat CLI](../reference/cli/chat.md) — `/tasks` /
  `/skill` / `/plan` slash commands
- FP-0011 (`docs/deep-dives/proposals/0011-remove-narrator.md`) —
  removed the dedicated narrator skill; router LLM narrates inline
- FP-0012 (`docs/deep-dives/proposals/0012-async-skill-execution.md`) —
  this design's full proposal
