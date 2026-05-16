# ADR-0034: A2A task lifecycle (FP-0001)

**Status**: Accepted (2026-05-16) — implementation lands in this PR series
**Track**: A2A protocol — async task support + ask_user round-trip

## Context

Reyn exposes each registered agent as an A2A-addressable peer (see
`docs/concepts/a2a.md`). The MVP `message/send` implementation is
**synchronous-with-timeout**: the caller POSTs a message and blocks waiting
for the agent's final reply, up to `DEFAULT_SEND_TIMEOUT_SECONDS`.

This is incompatible with skills that contain `ask_user` — a skill that
pauses mid-execution to ask the user a clarifying question cannot be served
by a single synchronous HTTP round-trip. The peer either times out, or receives
a placeholder reply, with no path to deliver the answer and resume the skill.

Three aspects compound the problem:

1. **Single-shot contract mismatch.** `message/send` was designed for "send
   text, get text back". A mid-run interrupt requires a separate answer
   channel.
2. **No run identity.** The existing endpoint returns a synchronous reply; it
   does not assign a stable `run_id` the peer could reference for subsequent
   operations (polling, answering, cancelling).
3. **No intervention routing.** `ChatSession`'s `InterventionBus` is
   process-local (TUI / CLI). There is no mechanism to route an `ask_user`
   event to an external HTTP client and receive the answer back.

## Considered alternatives

### Alt 1 — Extend the synchronous timeout

Increase `DEFAULT_SEND_TIMEOUT_SECONDS` so the skill has time to complete
and the peer does not time out. Rejected: the timeout cannot know when an
`ask_user` will be answered by a human peer; it could be hours. Infinite
timeout means the HTTP connection hangs indefinitely.

### Alt 2 — Webhook-only (no polling)

On `ask_user`, fire a webhook to the peer's provided URL and return 202
Accepted immediately. The peer delivers the answer by POSTing back to a
dedicated callback URL. No GET polling. Rejected as the primary path because
it requires the peer to have a reachable HTTP server, which not all
A2A callers can guarantee (e.g. CLI tools, scripted agents). Polling is
retained as a first-class path; webhook is retained as an optional
enhancement.

### Alt 3 — SSE from the initial POST (streaming)

Keep a single `message/send` request open as an SSE stream; the server sends
`ask_user` frames over the stream and the client replies via a parallel POST.
Rejected for this iteration: SSE from a JSON-RPC POST is non-standard
(A2A's `message/stream` method is separate); wiring bidirectional state through
SSE adds significant complexity with no gain over the polling path.

### Alt 4 — Separate `tasks/*` A2A methods

Use A2A's own task vocabulary (`tasks/get`, `tasks/cancel`) as first-class
JSON-RPC methods instead of Reyn-specific REST endpoints. Deferred: the A2A
spec's task lifecycle section is still evolving; Reyn-specific REST endpoints
(`GET /a2a/tasks/{run_id}`, `POST /a2a/tasks/{run_id}/cancel`) are adopted
now and can be mapped to A2A-spec methods later without breaking the
backing model.

## Decision

**Introduce `RunRegistry` + `A2AInterventionBus` + chain_id-scoped override
on `ChatSession`; overload `message/send` with `task_id` / `async_mode` /
`webhook_url`.**

### Components

**`RunRegistry`** (`src/reyn/web/run_registry.py`): in-memory
`run_id → RunEntry` map. `RunEntry` carries status (`running`,
`input-required`, `completed`, `failed`, `cancelled`), the pending
`UserIntervention` (= the `asyncio.Future` that unblocks the skill),
the buffered question text, the final result or error, an optional
`webhook_url`, and an event history list for SSE replay. Attached to
`app.state.run_registry` at server startup; lifetime = process lifetime
(crash recovery for async tasks is a follow-up FP).

**`A2AInterventionBus`** (`src/reyn/web/a2a_intervention.py`): implements
`InterventionBus.request(iv)`. When called, it:
1. Calls `registry.update(run_id, status="input-required", question=iv.prompt,
   pending_intervention=iv)`.
2. Fires a webhook POST if `entry.webhook_url` is set (fire-and-forget; errors
   logged, not raised).
3. Awaits `iv.future` — blocks until `registry.answer_intervention` resolves it.

**`ChatSession.register_intervention_override(chain_id, bus)`** (F1, already in
`src/reyn/chat/session.py`): registers a per-chain override bus so that any
`ask_user` in a skill running under `chain_id` is routed to `bus.request`
instead of the default TUI bus. `unregister_intervention_override` is called in
`finally` after the skill completes.

**Router extensions** (`src/reyn/web/routers/a2a.py`):

- `POST /a2a/agents/{name}` extended: if `params.async_mode=true` or
  `params.webhook_url` is set, spawns an asyncio.Task (registered in
  `RunRegistry`) and returns `{kind: "task", id: run_id, status: "running",
  agent_name: ...}` immediately. If `params.task_id` is set, resolves the
  pending intervention for that run via `registry.answer_intervention`.
- `GET /a2a/tasks/{run_id}` — returns `RunEntry.to_public_dict()`.
- `POST /a2a/tasks/{run_id}/cancel` — calls `registry.cancel(run_id)`.
- `GET /a2a/tasks/{run_id}/events` — SSE stream of `RunEntry.history_events`;
  closes on terminal status.

**Agent Card** flips `capabilities.streaming` and `capabilities.pushNotifications`
to `true` once the router extensions land.

### Wire protocol (summary)

```
1. POST /a2a/agents/{name}  {async_mode: true, message: ...}
   → {kind: "task", id: run_id, status: "running"}

2. GET  /a2a/tasks/{run_id}
   → {status: "input-required", question: "..."}

3. POST /a2a/agents/{name}  {task_id: run_id, message: "answer text"}
   → {answered: true}

4. GET  /a2a/tasks/{run_id}
   → {status: "completed", result: "..."}
```

## Consequences

**Positive:**

- A2A peers can now drive skills that contain `ask_user` — the primary
  motivation for FP-0001.
- `ChatSession` gains a clean per-chain intervention override hook that is
  generic (not A2A-specific), reusable by future callers.
- `RunRegistry` is fully in-memory and process-local — no schema changes,
  no WAL additions for this iteration.
- Webhook is fire-and-forget; a slow or unreachable peer webhook does not
  block the skill or the OS event loop.
- The existing synchronous `message/send` path is unaffected; peers that
  don't set `async_mode` continue to work as before.

**Negative:**

- `ChatSession` now carries `_intervention_overrides: dict[str, InterventionBus]`.
  The dict is keyed by `chain_id`; a long-lived process with many concurrent
  async tasks holds the map for the duration of each task.
- `RunRegistry` is not persisted. A server restart drops all in-flight async
  tasks; peers polling after a restart will receive 404 for the `run_id`.
  Crash recovery for async tasks is a follow-up FP.
- `asyncio.Task` cancellation (`POST /cancel`) is best-effort: cancellation
  propagates as `asyncio.CancelledError` but the skill's try/finally blocks
  may delay teardown.

**Precluded:**

- A2A spec-native `tasks/*` JSON-RPC methods without a mapping layer. Reyn's
  REST endpoints (`/a2a/tasks/{run_id}`) serve the same purpose; bridging to
  A2A-spec task methods is a future compatibility layer, not required now.

## References

- `src/reyn/web/run_registry.py` — `RunRegistry`, `RunEntry`
- `src/reyn/web/a2a_intervention.py` — `A2AInterventionBus`
- `src/reyn/web/notifications.py` — `post_webhook`
- `src/reyn/web/routers/a2a.py` — router extensions
- `src/reyn/chat/session.py` — `register_intervention_override`
- `docs/concepts/a2a.md` — user-facing protocol documentation
- ADR-0008 / ADR-0016 — intervention buffering precedent (`InterventionBus`
  contract that `A2AInterventionBus` implements)
