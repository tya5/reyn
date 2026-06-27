"""Task domain model — term-neutral (#1953).

States, origin, and the ``Task`` record. No backend / A2A / sqlite vocabulary
here — the A2A layer maps ``TaskState`` ↔ A2A states at its boundary (#1948),
and backends map this record to their own storage (sqlite table, gh issue, …).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskState(str, Enum):
    """Lifecycle — 7 base states (#2187 §3.4).

    ``unassigned`` = no assignee yet (the pending-assignment queue); ``blocked`` =
    DAG deps not all terminal; ``ready`` = DAG-unblocked + assigned but not yet
    started; ``running`` = the assignee is executing; ``done``/``failed``/``aborted``
    are terminal. "Waiting on children" / "deciding" are NOT base states — they are
    derived from the open-child counts (``N_awaited``/``N_background``) over a
    ``running`` task (#2187 §3.4). Soft-delete is the orthogonal retention dimension
    (``Task.archived_at``), not a state. A2A mapping lives in the A2A layer:
    ready→submitted, running→working, blocked→input-required/auth-required,
    done→completed, failed→failed, aborted→canceled.
    """

    UNASSIGNED = "unassigned"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


# Terminal states never transition further (single-writer is moot once here).
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.ABORTED}
)


def require_valid_status(status: str) -> "TaskState":
    """Validate ``status`` is a member of the 7-state ``TaskState`` (#2187 followup).

    The backend is the task-state MASTER — it must NEVER store an invalid status. A weak
    model that emits a stale ``"completed"`` (the pre-#2187 value) or any non-member is
    caught HERE, not silently written to corrupt the read (the dogfood found a sqlite
    write that stored a raw invalid string). The op-layer ``Literal`` constrains the LLM
    path; this is the universal data-integrity guard for EVERY write path (op / A2A /
    direct / test). Returns the ``TaskState``; raises ``ValueError`` listing the valid
    values otherwise."""
    try:
        return TaskState(status)
    except ValueError:
        valid = ", ".join(repr(s.value) for s in TaskState)
        raise ValueError(f"invalid task status {status!r} — must be one of: {valid}") from None


class TaskDepNotFoundError(Exception):
    """A dependency edge references a ``depends_on`` task that does not exist
    (#1953 slice 6, OQ-1). A dangling dep is an instant latent deadlock, so the
    edge is rejected at add-time. The op layer maps this to a decision-enabling
    ``status="error"`` result (never propagated through the op dispatcher)."""

    def __init__(self, task_id: str, depends_on: str) -> None:
        self.task_id = task_id
        self.depends_on = depends_on
        super().__init__(
            f"dependency {depends_on!r} of task {task_id!r} does not exist"
        )


class TaskCycleError(Exception):
    """Adding a dependency edge would create a cycle in the dependency DAG
    (#1953 slice 6, OQ-4/5) — rejected so the graph stays acyclic-by-construction
    (deadlock-impossible, §13). Carries the offending edge + the cycle node-path
    for a decision-enabling op result (never propagated through the dispatcher)."""

    def __init__(self, task_id: str, depends_on: str, path: list[str]) -> None:
        self.task_id = task_id
        self.depends_on = depends_on
        self.path = path
        super().__init__(
            f"edge {task_id!r}->{depends_on!r} would create a cycle: {' -> '.join(path)}"
        )


class TaskOrigin(str, Enum):
    """Origin decides deletion coupling (#1953 §17).

    ``self`` — agent's own working unit; requester == assignee == agent →
    deleted together with the agent (coupled), no external notify.
    ``external`` — A2A client / human / other system requested it; requester is
    external and persists → assignee-delete archives + notifies the requester.
    """

    SELF = "self"
    EXTERNAL = "external"


class TaskRequesterKind(str, Enum):
    """What kind of entity the ``requester`` routing-key names (#1953 §16
    recursive-request model).

    ``session`` — a session owns the request (the original model: a top-level
    task requested by a session; ``requester`` is a session routing-key).
    ``task`` — a *task-as-request* owns this sub-task; ``requester`` is that
    task's ``task_id``. Recovery routing resolves a ``task`` requester to its
    ASSIGNEE (the managing session) before waking — the recursive generalization
    of §16 S1 (route to the requester; if it is a task, resolve to its assignee).

    OS-SET at create from the caller's execution context + IMMUTABLE for the
    Task's life — no LLM/op sets or mutates it, so it cannot be mis-marked to
    mis-route a recovery (the §16 security invariant).
    """

    SESSION = "session"
    TASK = "task"


class TaskLinkType(str, Enum):
    """The child→parent decomposition-link type (#2187 §3.5).

    ``awaited`` — the parent BLOCKS on this child (needs its result): it gates the
    parent's completion (counts toward ``N_awaited``). ``background`` — the parent
    CONTINUES its own work alongside this child and never blocks on it (parallel).
    Marked at child creation, durable — the per-child wake-behaviour policy (#2187
    §4). Meaningful only for a decomposition child (``requester_kind=task``); a
    top-level task carries the default but it is never consulted.
    """

    AWAITED = "awaited"
    BACKGROUND = "background"


class ChildCounts(NamedTuple):
    """Open-child counts split by decomposition-link type (#2187 §3.4) — the derived
    dimension over a task's children. ``awaited`` children gate the parent's
    completion (RUNNING→DONE only when ``awaited + background == 0``); both reach the
    waker reconciler. Derived on-demand from the children's durable states, never
    separately stored."""

    awaited: int
    background: int


@dataclass
class Task:
    """One trackable work-unit.

    ``assignee`` is the **session identity** (#1814 routing-key) currently bound to
    execute the Task — the single-writer of ``status``. Under #2187 backend-master the
    binding is a **rebindable WAL subscription**, NOT an immutable field: it may be
    ``None`` (UNASSIGNED — the pending-assignment queue, §27-31) and changed via
    ``record_rebound`` (claim an UNASSIGNED task / owner-initiated reassign / re-queue),
    append-only so it stays P6/rewind-clean. The single-writer CAS is therefore
    ``caller_session_id == the CURRENT (hydrated) assignee`` — a read-then-check against
    the live WAL binding, not a birth-fixed equality. ``requester`` is the
    notify-target on disposition AND the ownership edge (§16 recursive-request: a
    task-as-request owns its sub-tasks, ``requester_kind=task``) — the sole
    decomposition relation now (the legacy ``parent_id`` tree was removed in §16
    slice C; ownership = the requester edge). ``deps`` are depends-on edges (the
    dependency DAG, §13) — kept here for the in-memory backend; the sqlite backend
    stores them in a ``task_links`` table.
    """

    task_id: str
    name: str
    assignee: str | None  # the bound executor session (#2187 rebindable binding); None = UNASSIGNED (pending-assignment queue, §27-31)
    requester: str
    requester_kind: TaskRequesterKind = TaskRequesterKind.SESSION  # §16: session-owned vs task-as-request owned (the ownership edge)
    link_type: TaskLinkType = TaskLinkType.AWAITED  # #2187 §3.5: this child's decomposition-link to its parent (awaited gates the parent; background runs parallel). CONTENT (backend column, like deps) — NOT the WAL binding. Meaningful only when requester_kind=task.
    origin: TaskOrigin = TaskOrigin.SELF
    status: TaskState = TaskState.READY
    description: str | None = None
    created_by: str | None = None  # audit provenance (§0-Q3); operative notify = requester
    awaiting_since: float | None = None  # R-D16 WAL-floor exclusion (set while blocked)
    archived_at: str | None = None  # soft-delete retention marker (#2187): orthogonal to the lifecycle state — set alongside ABORTED by abort(); the §24 purge-window + the list hidden-filter key on it
    deps: list[str] = field(default_factory=list)  # depends-on task_ids (DAG, §13)
    tools: list[str] = field(default_factory=list)  # narrowed tool set for the exec engine (#1953 slice P2)
    result: str | None = None  # exec-layer output captured on completion (#1953 slice P2)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        """JSON-safe projection (op return shape + backend round-trip)."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "assignee": self.assignee,
            "requester": self.requester,
            "requester_kind": self.requester_kind.value,
            "link_type": self.link_type.value,
            "origin": self.origin.value,
            "status": self.status.value,
            "archived_at": self.archived_at,
            "description": self.description,
            "created_by": self.created_by,
            "awaiting_since": self.awaiting_since,
            "deps": list(self.deps),
            "tools": list(self.tools),
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
