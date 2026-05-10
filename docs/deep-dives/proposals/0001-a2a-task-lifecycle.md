# FP-0001: A2A task lifecycle — ask_user / push notification support

**Status**: proposed
**Proposed**: 2026-05-09
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

The current A2A implementation supports only `message/send` (synchronous). When `ask_user` fires during skill execution, the only possible response is a timeout. By implementing a task lifecycle centered on `RunRegistry`, we can handle `ask_user` pause/resume, push notifications, and SSE streaming in a single unified solution.

---

## Motivation

### Current constraints

```
Client ──POST /a2a/{name}──▶ message/send starts
                              skill running...
                              ask_user fires ← execution stops here
◀── timeout / partial ──────  no path to inject an answer
```

- Skills containing `ask_user` are effectively unusable via A2A
- Clients have no way to learn about progress (no polling possible)
- The Agent Card declares `streaming: false`, `pushNotifications: false`,
  creating a clear capability gap compared to competitors
  (e.g., Hermes Agent's Checkpoints v2)

### Relationship with ACP

ACP (IBM BeeAI) has been integrated under the A2A umbrella (as of 2026-05).
ACP's `await_resume` model maps directly to the `ask_user` support proposed here.
Implementing this would allow both the A2A and ACP protocols to be served from the same foundation.

---

## Proposed implementation

### Core: RunRegistry

```python
# src/reyn/web/run_registry.py (new)
{
  run_id: {
    "task":        asyncio.Task,          # skill running in background
    "status":      "running" | "input-required" | "completed" | "failed",
    "question":    str | None,            # question text from ask_user
    "intervention": UserIntervention | None,  # Future awaiting answer
    "result":      str | None,
    "webhook_url": str | None,            # push notification destination
  }
}
```

### Flow (ask_user)

```
1. POST /a2a/{name}  →  issue run_id, start skill via asyncio.create_task()
2. ask_user fires    →  status = "input-required", store question in RunRegistry
3. GET tasks/{run_id} → {status: "input-required", question: "..."}
4. POST /a2a/{name} {task_id, answer}  →  InterventionBus.answer(text)
5. skill resumes → status = "completed", result stored
```

### Additional endpoints

| Endpoint | Purpose |
|---|---|
| `GET /a2a/tasks/{run_id}` | Poll task status and question text |
| `GET /a2a/tasks/{run_id}/events` | SSE stream (EventLog filtered by run_id) |
| `POST /a2a/tasks/{run_id}/cancel` | Cancel a task |

`message/send` gains a `task_id` parameter to serve both as a new task initiator and as an answer injector for existing tasks.

### Push notification

```python
async def _notify(run_id: str, status: str, payload: dict):
    reg = run_registry[run_id]
    reg["status"] = status
    if url := reg.get("webhook_url"):
        async with httpx.AsyncClient() as c:
            await c.post(url, json={"run_id": run_id, "status": status, **payload})
```

Trigger points: skill start / ask_user fires / skill completes / error — exactly 4 locations.

### Agent Card update

After implementation, update the following to `true`:

```python
"capabilities": {
    "streaming": True,           # SSE support
    "pushNotifications": True,   # webhook support
    "stateTransitionHistory": False,  # still not supported
}
```

---

## Dependencies

- `src/reyn/web/routers/a2a.py` (existing — target for modification)
- `src/reyn/user_intervention.py` / `InterventionBus` (existing — add bridge)
- `httpx` (likely already in the FastAPI project; add if not present)

Prerequisite PRs: none (can be implemented independently)

---

## Cost estimate

**Total: MEDIUM**

| Task | Cost | Notes |
|---|---|---|
| `RunRegistry` implementation | SMALL | in-memory dict + asyncio.Task management |
| Make `message/send` background | SMALL | switch to `create_task` only |
| `tasks/get` endpoint | SMALL | read-only Registry access |
| `InterventionBus` bridge | MEDIUM | hook to update Registry when ask_user fires |
| Push notification | SMALL | one httpx.post call |
| SSE streaming | SMALL | FastAPI StreamingResponse + EventLog.subscribe |
| `tasks/cancel` | SMALL | asyncio.Task.cancel() |
| Agent Card update | SMALL | change capabilities flags |

The only bottleneck is the **InterventionBus bridge**. Everything else chains as SMALL.

---

## Related

- `src/reyn/web/routers/a2a.py` — existing A2A implementation (see MVP comments)
- `src/reyn/user_intervention.py` — InterventionBus implementation
- `docs/concepts/a2a.md` — A2A concept documentation
- ACP OpenAPI spec: https://github.com/i-am-bee/acp/blob/main/docs/spec/openapi.yaml
