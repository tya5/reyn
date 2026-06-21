"""Task backend contract + in-memory implementation (#1953 slice 1).

``TaskBackend`` is the **tracker-agnostic core contract** every backend must
implement (id / status / assignee / requester / links / fields). ``InMemoryTaskBackend``
is the test / ephemeral backend; the durable sqlite backend lives in
``sqlite_backend.py``. Later slices layer cascades, cycle-checks, and budget on top.

Single-writer (settled model, #1953): a Task is owned by its **assignee session**
(the #1814 per-contextId routing-key), and ``assignee`` is immutable — so the
single-writer CAS is a **fixed equality** ``assignee == caller_session_id``, NOT
a permission gate (the permission system is resource-scoped, no caller identity
at op-exec) and NOT a skill-run claim (a multi-turn Task spans many skill-runs,
so no one run owns it). ``caller_session_id`` is ``OpContext.session_id``,
threaded down the runtime chain. No claim token / version is needed (the key is
fixed at create). A non-assignee write raises ``PermissionError``.

Enforcement deferred to later slices:
  - abort 3-step (``cancel_inflight → await_quiescent → terminal`` + seq-fence,
    needs Session access) + abort=delete unification → PR-2 / later.
  - deletion cascade (DOWN-abort children / UP-notify requester) → later.
  - dependency cycle-check + readiness propagation → slice 6.
  - unblock-predicate evaluation → slice 7.
"""
from __future__ import annotations

from typing import Protocol

from reyn.task.model import TERMINAL_STATES, Task, TaskState, _now_iso


class TaskBackend(Protocol):
    """The contract a Task backend implements. Async so a remote/db backend
    (sqlite, gh-issue, jira) can do I/O without changing callers."""

    async def create(self, task: Task) -> Task: ...

    async def get(self, task_id: str) -> Task | None: ...

    async def list(
        self,
        *,
        assignee: str | None = None,
        requester: str | None = None,
        status: str | None = None,
        parent_id: str | None = None,
    ) -> list[Task]: ...

    async def update_status(
        self, task_id: str, status: str, *, caller_session_id: str | None = None
    ) -> Task | None:
        """Transition status. Single-writer CAS: succeeds only when ``caller_session_id
        == task.assignee`` (immutable assignee, fixed equality); a non-assignee
        caller raises ``PermissionError``. Returns None for an unknown task."""
        ...

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None: ...

    async def abort(self, task_id: str, reason: str | None = None) -> list[Task]: ...

    async def set_awaiting(self, task_id: str, awaiting_since: float | None) -> Task | None: ...

    async def set_unblock_predicate(self, task_id: str, predicate: str) -> Task | None: ...

    async def add_comment(self, task_id: str, author: str, body: str) -> str | None: ...


class InMemoryTaskBackend:
    """Process-local dict-backed backend (slice 1 stub + the ``in-memory``
    config option for tests). Not durable, not crash-safe — sqlite (slice 2)
    is the first durable backend. Single-threaded async; no locking needed
    because slice 1 does not yet enforce CAS."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        # slice-1 stub stores for ops whose enforcement lands later.
        self._predicates: dict[str, str] = {}
        self._comments: dict[str, list[dict]] = {}
        self._comment_seq = 0

    async def create(self, task: Task) -> Task:
        self._tasks[task.task_id] = task
        return task

    async def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list(
        self,
        *,
        assignee: str | None = None,
        requester: str | None = None,
        status: str | None = None,
        parent_id: str | None = None,
    ) -> list[Task]:
        out = list(self._tasks.values())
        if assignee is not None:
            out = [t for t in out if t.assignee == assignee]
        if requester is not None:
            out = [t for t in out if t.requester == requester]
        if status is not None:
            out = [t for t in out if t.status.value == status]
        if parent_id is not None:
            out = [t for t in out if t.parent_id == parent_id]
        return out

    async def update_status(
        self, task_id: str, status: str, *, caller_session_id: str | None = None
    ) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # Single-writer CAS: only the assignee session may write (fixed equality).
        if task.assignee != caller_session_id:
            raise PermissionError(
                f"task {task_id!r} status-write rejected: caller "
                f"{caller_session_id!r} is not the assignee {task.assignee!r} "
                f"(single-writer)"
            )
        # Terminal-guard: a terminal task takes no further transitions — this is
        # the abort straggler-reject (Option B, #1953): an assignee write after
        # the requester aborts the task is rejected, so no straggler lands.
        if task.status in TERMINAL_STATES:
            raise PermissionError(
                f"task {task_id!r} status-write rejected: task is terminal "
                f"({task.status.value}) — no further transitions (e.g. post-abort straggler)"
            )
        task.status = TaskState(status)
        task.updated_at = _now_iso()
        return task

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # slice 6 adds cycle-check + readiness propagation; slice 1 records the edge.
        if depends_on not in task.deps:
            task.deps.append(depends_on)
        task.updated_at = _now_iso()
        return task

    async def abort(self, task_id: str, reason: str | None = None) -> list[Task]:
        # abort = delete (cooperative-terminal, Option B): archive this task + its
        # whole sub-tree (DOWN-cascade, §18). No forced cancel — the assignee's
        # in-flight work is rejected by the terminal state at its next status-write.
        # Returns the aborted tasks (root first) so the caller can emit a
        # disposition event per task (UP-notify, 2b-2); [] if no such task.
        if task_id not in self._tasks:
            return []
        subtree: list[str] = [task_id]
        frontier = [task_id]
        while frontier:
            pid = frontier.pop()
            for tid, t in self._tasks.items():
                if t.parent_id == pid and tid not in subtree:
                    subtree.append(tid)
                    frontier.append(tid)
        aborted: list[Task] = []
        for tid in subtree:
            self._tasks[tid].status = TaskState.ARCHIVED
            self._tasks[tid].updated_at = _now_iso()
            aborted.append(self._tasks[tid])
        return aborted

    async def set_awaiting(self, task_id: str, awaiting_since: float | None) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.awaiting_since = awaiting_since
        task.updated_at = _now_iso()
        return task

    async def set_unblock_predicate(self, task_id: str, predicate: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        self._predicates[task_id] = predicate
        return task

    async def add_comment(self, task_id: str, author: str, body: str) -> str | None:
        if task_id not in self._tasks:
            return None
        self._comment_seq += 1
        comment_id = f"c{self._comment_seq}"
        self._comments.setdefault(task_id, []).append(
            {"id": comment_id, "author": author, "body": body, "ts": _now_iso()}
        )
        return comment_id
