# Tasks — the dynamic work-unit model

A **Task** is a first-class, durable unit of work an agent can create, track,
delegate, and depend on — created and managed **dynamically, mid-flow**, by the
LLM through ordinary tool calls. It replaces the former upfront *planner* (a
single tool that pre-declared a fixed 2–7 step plan): instead of committing to a
plan before starting, an agent decomposes as it goes, adds dependencies when it
discovers them, and delegates sub-tasks to other sessions.

## Why dynamic, not upfront

The planner asked the LLM to pre-select an orchestration tool and emit a complete
plan before any work — a shape a weaker model rarely produces well, and that
can't adapt once execution reveals the real structure. The task model exposes
small, composable ops the LLM reaches for *when the need appears*: create a
sub-task, order two of them, mark one done, abort a sub-tree. Adoption is driven
by the system prompt's routing guide ("multi-target / multi-step work → decompose
into sub-tasks") plus the catalog, not by a forced mode.

## The model

- **Work-unit identity.** Each Task has a `task_id`, a `name`, an optional
  `description`, and a `status` (lifecycle: `unassigned` → `ready` → `running`
  → `done` / `failed`; `blocked` while deps are unmet; `aborted` on abort.
  `archived_at` is a retention field on the task record — set alongside `aborted`
  by abort — not a lifecycle state).
- **Requester vs assignee.** The **requester** is the session that created the
  Task (the notify-target). The **assignee** is the single worker session and is
  **immutable** for the Task's life (no hand-off). `assignee` defaults to the
  caller (a self-task); a different value **delegates** the Task cross-session.
- **Single-writer CAS.** Only the **assignee session** may write a Task's status
  — enforced by a fixed-equality compare-and-set (`assignee == caller session
  id`) in the backend. The caller session id is the `OpContext.session_id`
  routing key, threaded by the OS (never an op field). A terminal Task rejects
  all further writes (the cooperative-terminal guard). Topology writes
  (dependencies, abort) are owned by the **requester**.
- **Dependency DAG.** `deps` are depends-on edges. A Task born with unmet deps is
  OS-derived `blocked`; readiness is recomputed (never written directly) as deps
  complete. Edges are existence- and cycle-checked. The requester can `repoint` a
  dependent at a substitute — the primary recovery move.
- **Child link type and completion-join.** A decomposition child carries a
  `link_type` (`awaited` or `background`). `awaited` means the parent needs that
  child's result and blocks on it — it gates the parent's `running → done`
  transition. `background` means the parent continues in parallel and never waits.
  A task transitions `running → done` only when both `awaited` and `background`
  open-child counts reach zero (the completion-join gate).
- **Backend as external state master.** The task backend (sqlite by default) is the
  external master of task state — it holds each task's `status`, DAG, and content.
  Reyn acts as a client: it sends state-change requests to the backend and
  subscribes to the state-change events the backend publishes. The task↔session
  binding (`assignee`, `requester`) is Reyn-internal and lives in the WAL
  (StateLog), not in the backend — that binding is what gets rewound on
  time-travel, while the backend's task-state is re-read as current external truth.

## The ops

The 11 ops are callable both from a phase's control-IR and, since the dynamic
wiring, from the chat router via `invoke_action` (`task__create`,
`task__update_status`, …). The router path enforces the **same** assignee CAS as
the phase path — keyed on the real caller session id, with no bypass (the bridge
refuses rather than run a session-less context that would mask the gate).

| Op | Role gate | Purpose |
|---|---|---|
| `task.create` | requester = self | Create a (sub-)task; `deps` order it, `assignee` delegates; sub-task ownership is OS-derived from execution context (§16) |
| `task.update_status` | **assignee** (CAS) | Declare a status transition (single writer) |
| `task.get` / `task.list` | — | Read one record / list (by assignee / requester / status); `requester=<task-id>` lists sub-tasks owned by that task |
| `task.add_dependency` / `task.remove_dependency` | requester | Add / drop a depends-on edge |
| `task.repoint_dependency` | requester | Atomically repoint an edge to a substitute (cycle-checked first) |
| `task.abort` | requester | Move a Task and its sub-tree to `aborted` and set `archived_at` (cooperative-terminal, down-cascade) |
| `task.heartbeat` | assignee | Liveness + unblock-predicate evaluation trigger |
| `task.register_unblock_predicate` | assignee | Register a deterministic (no-LLM) unblock predicate |
| `task.comment` | — | Append to the Task's thread (inter-agent / human-in-the-loop) |

The ToolDefinitions are derived single-source from the IROp models
(`model_json_schema()` minus the `kind` discriminator), so the LLM-facing schema
never drifts from the runtime contract.

## When to use

- **Multi-target / iteration** ("do X for each Y", "process N files"): one
  sub-task per target plus a final aggregate task that `deps` on the rest.
- **Multi-step work worth tracking**: create sub-tasks and update their status so
  progress is durable across turns and crashes.
- **Delegation**: create a sub-task with another session as `assignee` to hand
  work to a peer agent (the worker is the single writer of its status).

## See also

- [Workspace](workspace.md) — the single source of truth for data passed between phases
- [Events](events.md) — the runtime's per-run audit trail
- [Permission model](permission-model.md) — the gate layer the ops resolve through
