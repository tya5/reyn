# FP-0012: Async Skill/Agent/Plan Execution — Non-blocking Long-running Tasks

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Skills, agent delegations, and plans are all designed for long-running execution (minutes to
hours), yet `invoke_skill` currently blocks the session's message loop via
`await _run_skill_awaitable()`. Every user message typed during skill execution queues in the
inbox and goes unprocessed until the skill finishes. Change `invoke_skill` to fire-and-forget
(async dispatch), return a spawned status immediately to the router LLM, and re-enter the
router with the result when the task completes — without narrator.

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

### Phase 1 — invoke_skill becomes async dispatch

**`invoke_skill` tool returns immediately after spawning:**

```python
# _handle in invoke_skill.py — after validation
task = asyncio.create_task(
    session._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
)
session.running_skills[run_id] = task

return {
    "status": "spawned",
    "run_id": run_id,
    "note": "Running in the background. I will notify you when it completes.",
}
```

`invoke_skill` is registered with `dispatch_kind="async"`, so the router loop exits
immediately (same branch as `delegate_to_agent`). The router LLM never sees the tool result
inline — instead it sees the exit and generates a user-facing acknowledgment:

```
Router → user:
  "Starting skill_builder. I'll let you know when it's done.
   You can check progress with /skill list."
```

The session loop is now free to process the next inbox message immediately.

**Router system prompt addition:**

```
- After invoke_skill spawns a task: tell the user what you started and that
  you will notify them on completion. Mention /skill list for progress.
  Do NOT ask follow-up questions until the task finishes.
```

### Phase 2 — Completion re-entry into router (no narrator)

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

`_handle_skill_completed` runs a single router LLM turn with the result injected as context:

```
[system addendum]:
  An async task you started has completed.
  skill: skill_builder
  status: finished
  result: {"skill_name": "my_skill", "path": "reyn/project/my_skill/skill.md"}

  Summarize what completed for the user in 1–2 sentences.
  Status guidance (same as FP-0011 Component B).
```

Router LLM generates the narration → pushed to user outbox. This replaces narrator entirely
(subsumes FP-0011's completion narration path).

**Why router re-entry, not narrator?**

Consistent with FP-0011: the router LLM already narrates every other tool result inline
(recall, list_skills, etc.). Skill completion is structurally identical — a tool result
that needs natural language narration. Having one narration path (router LLM) is cleaner
than two (router for sync tools + narrator for skills).

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
  └─ RouterLoop: invoke_skill(name="skill_builder") → spawns task, returns immediately
  └─ Router LLM → user: "skill_builder を起動しました。/tasks で進捗を確認できます。"
  └─ Session loop: free — processes next user message immediately

User: "ちなみに recall の設定どうなってた？"
  └─ RouterLoop: recall(...) → router LLM answers inline
  └─ User sees answer — skill is still running in background

[2 minutes later] skill_builder completes
  └─ inbox: ("skill_completed", {skill: "skill_builder", status: "finished", data: {...}})
  └─ _handle_skill_completed → router LLM turn with result context
  └─ Router LLM → user: "skill_builder が完了しました。reyn/project/my_skill/ に作成されました。"
```

---

## Proposed implementation

### Component A — `invoke_skill` async dispatch (MEDIUM)

- Change `INVOKE_SKILL` to `dispatch_kind="async"`
- `_handle` spawns `create_task` and returns `{"status": "spawned", "run_id": ..., "note": ...}`
- Remove `_run_skill_awaitable` (or keep as internal utility for narrator removal)
- Wire `_run_one_skill` to enqueue `"skill_completed"` into inbox on completion

### Component B — `session.run()` loop handles `"skill_completed"` (SMALL)

- Add `elif kind == "skill_completed"` branch
- `_handle_skill_completed` builds a compact router context and runs one router LLM turn
- Router generates narration → outbox

### Component C — Router system prompt updates (SMALL)

- Post-`invoke_skill` acknowledgment guidance (Phase 1)
- Post-completion narration guidance (Phase 2, same as FP-0011 Component B)

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
- `dispatch_kind="async"` pattern — already used by `delegate_to_agent`
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
