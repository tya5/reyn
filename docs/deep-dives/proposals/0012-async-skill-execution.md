# FP-0012: Async Skill/Agent/Plan Execution — Non-blocking Long-running Tasks

**Status**: **LANDED 2026-05-10** (Components A+B+C+D+E in a single commit;
chat-mode invoke_skill is now non-blocking, plan-mode keeps blocking
semantics for sequential step execution).
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

## Landing notes (2026-05-10)

Implemented per the proposal with these context-driven refinements:

- **`run_skill_fn` retained alongside new `spawn_skill_fn`**: chat-mode
  RouterLoops bind `spawn_skill_fn` (= non-blocking) and invoke_skill
  prefers it; plan-mode RouterLoops bind only `run_skill_fn` (= blocking)
  so per-step sequential synthesis still works. invoke_skill's `_handle`
  picks spawn over run when both are wired (chat-mode); plan-mode falls
  through to the blocking path.
- **`_run_skill_awaitable` not deleted**: kept as the plan-mode blocking
  call site. The FP-0011 contract test was reframed but not removed; a
  new `test_spawn_skill_for_router_returns_spawn_ack` covers the chat
  path.
- **Anti-optimism rule preserved**: the FP-0011 Component B
  strengthening (= MUST surface `data.error` / `result.error`
  verbatim) is retained in the new Component C `[task_completed]`
  narration block.
- **No G4 spike**: 5-track pre-fix multi-agent context analysis was
  used instead (memory `feedback_pre_fix_context_analysis.md` pattern,
  batch 22 lift). Tracks 1-5 audited invoke_skill flow, inbox
  architecture, dead-vs-live infra, chain_id correlation, and test
  surface. Findings flipped the proposal's "infra is already there as
  dead code" framing to "the dicts are LIVE; the actual gap is in
  invoke_skill's handler + session.run() loop wiring".
- **Follow-up dogfood**: N≥10 chat-mode session test recommended to
  verify (a) router LLM produces sensible spawn-ack text on weak +
  strong tier, (b) `[task_completed]` narration extracts the right
  fields, (c) anti-optimism rule fires on synthesised error completions.

---

## Summary

Skills, agent delegations, and plans are all designed for long-running execution (minutes to
hours), yet `invoke_skill` currently blocks the session's message loop via
`await _run_skill_awaitable()`. Every user message typed during skill execution queues in the
inbox and goes unprocessed until the skill finishes.

Change `invoke_skill` so its `_handle` spawns the task and returns
`{"status": "spawned", "run_id": ..., "chain_id": ...}` immediately (non-blocking, no
`dispatch_kind` change needed). The router LLM sees this tool result inline and acknowledges
to the user. When the task completes, a `user`-role message carrying the `chain_id` and
result is injected into the existing conversation thread — preserving full context so the
router LLM can correlate the completion with the original invocation and narrate accurately.

---

## Motivation

### The blocking problem

```
Session.run() main loop — sequential, single consumer
─────────────────────────────────────────────────────
kind, payload = await _consume_inbox()          ← blocked here during skill run
await _handle_user_message()
  └─ RouterLoop.run()
       └─ await invoke_skill tool
            └─ await _run_skill_awaitable()
                 └─ await agent.run()           ← minutes pass here
                                                   user types 3 messages
                                                   → all queue in inbox
                                                   → none are processed
```

For a 5-minute skill run, the user's chat is effectively frozen. Any messages typed are
silently deferred — there is no acknowledgment, no progress feedback, no way to interact.

### The fix already exists — as dead code

`_dispatch_routing_decision_for_user` (never called) uses `asyncio.create_task(_run_one_skill(...))`
— the correct fire-and-forget pattern. The infrastructure (`running_skills` dict,
`running_skills_started_at`, `running_skills_chain`) is already there. Slash commands
`/skill list` and `/skill discard` already work against this dict. The missing piece is
wiring `invoke_skill` to use this path and delivering the completion result back to the
router LLM.

### Scope: skill + agent delegation + plan

All three long-running operation types should be non-blocking. `delegate_to_agent` already
uses `dispatch_kind="async"`. Plans already use `create_task`. This FP focuses on
`invoke_skill` (the only remaining blocking case) and unifies the task management UX.

---

## Proposed design

### Phase 1 — invoke_skill becomes non-blocking (spawn-and-return)

**`_handle` spawns the task and returns immediately — no `dispatch_kind` change:**

```python
# _handle in invoke_skill.py — after validation
task = asyncio.create_task(
    session._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
)
session.running_skills[run_id] = task

return {
    "status": "spawned",
    "run_id": run_id,
    "chain_id": chain_id,   # ← router LLM will use this to correlate completion
    "note": "Running in the background. I will notify you when it completes.",
}
```

`dispatch_kind` remains `"sync"`. The router loop receives the tool result inline and
calls the router LLM one final time. The LLM sees `{status: "spawned", chain_id: "abc123"}`
and generates a user-facing acknowledgment:

```
Router → user:
  "Starting skill_builder (chain_id: abc123). I'll let you know when it's done.
   You can check progress with /skill list."
```

The background task is already running; the session loop is free to process the next inbox
message immediately after the router responds.

**Router system prompt addition:**

```
- When invoke_skill returns {status: "spawned", chain_id: ...}: tell the user what you
  started and that you will notify them on completion. Include the chain_id so they can
  reference it with /tasks status. Do NOT ask follow-up questions until the task finishes.
```

### Phase 2 — Completion re-entry via user message injection (no narrator)

#### Why tool_result cannot be used for async completion

LLM APIs (both OpenAI Chat Completions and Anthropic Messages API) enforce a strict
constraint: a `tool_result` / `role: "tool"` message must be preceded by an `assistant`
message containing the matching `tool_use` / `tool_calls` block. Injecting a `tool_result`
without a prior correlated `tool_use` returns a 400 error. By the time the async task
completes, the conversation has moved on; the original `tool_use` for `invoke_skill` already
has its `tool_result` (the `{"status": "spawned"}` response). There is no open `tool_use`
to correlate a second result with.

This is why no major multi-agent framework delivers truly async completion back to the LLM
as a tool_result — they either block until completion or start a fresh, context-free LLM
turn.

#### The correct approach: user message injection with chain_id

When `_run_one_skill` completes, instead of calling `_invoke_narrator`, enqueue a
`"skill_completed"` message into the session inbox:

```python
# _run_one_skill — on completion
await self._put_inbox("skill_completed", {
    "run_id": run_id,
    "skill": skill_name,
    "status": result.status,
    "data": result.data,
    "chain_id": chain_id,
})
```

The `session.run()` loop picks this up like any other inbox message:

```python
elif kind == "skill_completed":
    await self._handle_skill_completed(payload)
```

`_handle_skill_completed` injects a `user`-role message into the **existing conversation
thread** and runs one router LLM turn:

```python
# Injected into session message history (role="user")
"[task_completed] chain_id=abc123\n"
"skill: skill_builder  status: finished\n"
"result: {\"skill_name\": \"my_skill\", \"path\": \"reyn/project/my_skill/skill.md\"}\n\n"
"Please summarize what completed for the user in 1–2 sentences."
```

Router LLM generates the narration → pushed to user outbox.

**Why user message injection, not system addendum in a fresh LLM call?**

Injecting into the existing thread means the router LLM has full conversation context:
it can see the original `invoke_skill` call, the `{status: "spawned", chain_id: "abc123"}`
tool result, any exchanges with the user in between, and now the completion — all in one
coherent thread. The `chain_id` ties the completion notification back to the specific
invocation. A fresh, context-free LLM call loses all of this and produces lower-quality
narration. This approach is also the only one that correctly handles multiple concurrent
skills: each completion message carries its own `chain_id`, and the LLM can distinguish
which task finished.

### Phase 3 — Slash command enhancements

Existing commands (`/skill list`, `/skill discard`) are preserved and enhanced.
A new unified `/tasks` entry point spans skills, plans, and agent delegations:

```
/tasks                         → list all running tasks (skills + plans + delegations)
/tasks kill <run_id_prefix>    → cancel a specific task (wraps /skill discard)
/tasks status <run_id_prefix>  → show current phase, elapsed time, last P6 event
```

**`/tasks status` output:**

```
skill_builder [abc1]  running  2m 14s
  phase:    apply_improvements (3/5 iterations)
  last op:  write_file reyn/project/my_skill/phases/plan.md
  cost:     $0.08
```

This reads from `running_skills`, `running_skills_started_at`, and the live P6 event log.
No new session state is required.

---

## Message flow (after this FP)

```
User: "skill_builder を動かして"
  └─ RouterLoop: invoke_skill(name="skill_builder")
       └─ _handle: create_task(...) → returns {status:"spawned", chain_id:"abc123"} immediately
       └─ Router LLM sees tool_result inline, generates acknowledgment
  └─ Router LLM → user: "skill_builder を起動しました (chain_id: abc123)。/tasks で進捗を確認できます。"
  └─ Session loop: free — processes next user message immediately

User: "ちなみに recall の設定どうなってた？"
  └─ RouterLoop: recall(...) → router LLM answers inline
  └─ User sees answer — skill is still running in background

[2 minutes later] skill_builder completes
  └─ inbox: ("skill_completed", {skill:"skill_builder", chain_id:"abc123", status:"finished", data:{...}})
  └─ _handle_skill_completed:
       └─ injects user message into conversation thread:
            "[task_completed] chain_id=abc123 / skill: skill_builder / status: finished
             result: {skill_name: my_skill, path: reyn/project/my_skill/skill.md}
             Please summarize for the user in 1–2 sentences."
       └─ runs one router LLM turn (LLM has full thread context including original spawn)
  └─ Router LLM → user: "skill_builder が完了しました。reyn/project/my_skill/ に作成されました。"
```

---

## Proposed implementation

### Component A — `invoke_skill` non-blocking spawn (MEDIUM)

- `_handle` spawns `create_task` and returns **immediately** with
  `{"status": "spawned", "run_id": ..., "chain_id": ..., "note": ...}`
- **No `dispatch_kind` change** — remains `"sync"` so the router LLM receives the
  tool result inline and generates an acknowledgment in the same turn
- Remove `_run_skill_awaitable` (or keep as internal utility)
- Wire `_run_one_skill` completion to enqueue `"skill_completed"` into inbox

### Component B — `session.run()` loop handles `"skill_completed"` (SMALL)

- Add `elif kind == "skill_completed"` branch
- `_handle_skill_completed` injects a `user`-role message carrying `chain_id` +
  result into the **existing** conversation thread, then runs one router LLM turn
- Router sees full thread context (original spawn + intermediate exchanges +
  completion); generates narration → outbox

### Component C — Router system prompt updates (SMALL)

- Post-`invoke_skill` spawn guidance: acknowledge what started, include chain_id,
  mention `/tasks status` for progress (Phase 1)
- Post-completion narration guidance: when seeing `[task_completed]` user message,
  narrate in 1–2 sentences with status-aware wording (Phase 2)

### Component D — `/tasks` slash command (SMALL)

- New `slash/tasks.py` with `list` / `kill` / `status` subcommands
- Reads from existing `running_skills` + `running_skills_started_at` + P6 event log
- `/skill discard` kept as alias; `/plan discard` kept as alias

### Component E — Remove `_run_skill_awaitable` and dead code (SMALL)

- Delete `_dispatch_routing_decision_for_user` (confirmed dead code)
- Delete or internalize `_run_skill_awaitable` (no longer called from router path)
- Subsumes FP-0011 Component A (narrator call removal from `_run_skill_awaitable`)

---

## Relationship to FP-0011

FP-0011 (narrator removal) and FP-0012 address overlapping concerns:

| Concern | FP-0011 | FP-0012 |
|---|---|---|
| Remove narrator calls from `_run_skill_awaitable` | Component A | Component E (subsumed) |
| Router LLM narrates skill completion | Component B | Component B (Phase 2) |
| Delete `skill_narrator` skill | Component C | out of scope — can land independently |
| Remove narrator tests | Component D | out of scope — can land independently |
| Non-blocking skill execution | not addressed | **core goal** |
| Completion re-entry via inbox | not addressed | Component B |
| `/tasks` slash commands | not addressed | Component D |

**Recommendation**: FP-0011 Components C/D (delete skill and tests) can land first as
cleanup. FP-0012 Components A–E deliver the full async execution model.

---

## Dependencies

- `asyncio.create_task` + `running_skills` dict — already present in session.py
- `chain_id` — already present in session.py (`running_skills_chain`)
- P6 event log — used by `/tasks status` (no new events needed beyond existing ones)
- FP-0011 — partial overlap; see relationship table above

---

## Cost estimate

**Total: LARGE**

| Task | Cost | Notes |
|---|---|---|
| Component A: invoke_skill async dispatch | MEDIUM | Core change; touches invoke_skill.py + session.py |
| Component B: skill_completed inbox + handler | SMALL | New inbox kind + one router turn |
| Component C: router SP updates | SMALL | ~10 lines |
| Component D: `/tasks` slash command | SMALL | New slash/tasks.py; reads existing state |
| Component E: dead code removal | SMALL | Delete `_dispatch_routing_decision_for_user` |
| Tests | MEDIUM | Tier 2: session loop non-blocking contract; Tier 2: completion re-entry |

Risk: router LLM quality on spawned-task acknowledgment and completion narration (weak
model may not handle the new message types reliably). G4 spike recommended before landing.

---

## Related

- `src/reyn/chat/session.py` — `_run_skill_awaitable`, `_dispatch_routing_decision_for_user`, `run()` loop
- `src/reyn/tools/invoke_skill.py` — `INVOKE_SKILL` dispatch_kind
- `src/reyn/chat/slash/skill.py` — existing `/skill list` and `/skill discard`
- `src/reyn/chat/router_system_prompt.py` — Component C insertion points
- FP-0011 (`0011-remove-narrator.md`) — narrator removal; partially subsumed by this FP
