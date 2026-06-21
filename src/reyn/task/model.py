"""Task domain model — term-neutral (#1953).

States, origin, and the ``Task`` record. No backend / A2A / sqlite vocabulary
here — the A2A layer maps ``TaskState`` ↔ A2A states at its boundary (#1948),
and backends map this record to their own storage (sqlite table, gh issue, …).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskState(str, Enum):
    """Lifecycle states (#1953 §0-Q1).

    ``ready`` = DAG-unblocked but not yet started; ``archived`` = soft-deleted
    (WAL-window auto-purge eligible, §24). A2A mapping lives in the A2A layer:
    ready→submitted, in_progress→working, blocked→input-required/auth-required,
    completed→completed, failed→failed, aborted→canceled, archived→(internal).
    """

    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    ARCHIVED = "archived"


# Terminal states never transition further (single-writer is moot once here).
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.ABORTED, TaskState.ARCHIVED}
)


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


@dataclass
class Task:
    """One trackable work-unit.

    ``assignee`` is the **session identity** (#1814 routing-key) that owns the
    Task — the single-writer of ``status``, immutable for the Task's life (no
    handoff — delegation is sub-task decomposition, §12). Because ``assignee`` is
    immutable, the single-writer CAS is a fixed equality ``assignee ==
    caller_session_id`` (no claim token / version needed). ``requester`` is the
    notify-target on disposition (§16). ``deps`` are depends-on edges (the
    dependency DAG, §13) — kept here for the in-memory backend; the sqlite backend
    stores them in a ``task_links`` table.
    """

    task_id: str
    name: str
    assignee: str
    requester: str
    origin: TaskOrigin = TaskOrigin.SELF
    status: TaskState = TaskState.PENDING
    description: str | None = None
    created_by: str | None = None  # audit provenance (§0-Q3); operative notify = requester
    parent_id: str | None = None  # ownership tree (§12), distinct from deps DAG
    budget_cap: float | None = None  # per-task budget (§8)
    cost_accum: float = 0.0
    awaiting_since: float | None = None  # R-D16 WAL-floor exclusion (set while blocked)
    deps: list[str] = field(default_factory=list)  # depends-on task_ids (DAG, §13)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        """JSON-safe projection (op return shape + backend round-trip)."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "assignee": self.assignee,
            "requester": self.requester,
            "origin": self.origin.value,
            "status": self.status.value,
            "description": self.description,
            "created_by": self.created_by,
            "parent_id": self.parent_id,
            "budget_cap": self.budget_cap,
            "cost_accum": self.cost_accum,
            "awaiting_since": self.awaiting_since,
            "deps": list(self.deps),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
