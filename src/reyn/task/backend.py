"""Task backend contract + in-memory implementation (#1953 slice 1).

``TaskBackend`` is the **tracker-agnostic core contract** every backend must
implement (id / status / assignee / requester / links / fields). Slice 1 ships
``InMemoryTaskBackend`` (the stub the design calls for); slice 2 adds the sqlite
backend (single-writer CAS via ``BEGIN IMMEDIATE`` + ``current_run_id``), and
later slices layer P6 events, cascades, cycle-checks, and budget on top.

Scope note (slice 1): these methods provide the *contract surface* — basic
persistence + retrieval. Enforcement that the design assigns to later slices is
intentionally NOT here yet:
  - **single-writer CAS reject** → slice 3. NOTE (part-3 audit C2): this is a
    **backend-CAS on ``current_run_id``**, NOT a permission gate — the permission
    system has no caller/session identity at op-exec time, so "caller≠assignee →
    reject via P5" is unimplementable. The writer-token is the caller's durable
    skill-run ``run_id`` (threaded from ``OpContext.run_id``, never an LLM param
    → unforgeable); ``update_status`` carries it now so slice 3 can CAS on
    ``current_run_id == writer_token`` (Hermes ``current_run_id`` CAS, same form).
  - **abort** → slice 3. NOTE (audit C1): abort is *cooperative*, not SIGKILL —
    ``cancel_inflight()`` only takes effect at the next tool-boundary, so an
    in-flight write can still land. The contract is the 3-step
    ``cancel_inflight → await_quiescent → terminal`` (+ seq-fence) so no write
    lands after the terminal transition. The stub here just sets the terminal
    state; the quiescence step needs Session access (slice 3).
  - deletion cascade (DOWN-abort children / UP-notify requester) → slice 3.
  - dependency cycle-check + readiness propagation → slice 6.
  - unblock-predicate evaluation → slice 7.
"""
from __future__ import annotations

from typing import Protocol

from reyn.task.model import Task, TaskState, _now_iso


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
        self, task_id: str, status: str, *, writer_token: str | None = None
    ) -> Task | None:
        """Transition status. ``writer_token`` is the caller's durable skill-run
        run_id (the single-writer claim token, audit C2). Slice 3 CAS-rejects when
        ``current_run_id`` is set and ``!= writer_token``; slice 1 accepts it."""
        ...

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None: ...

    async def archive(self, task_id: str) -> Task | None: ...

    async def abort(self, task_id: str, reason: str | None = None) -> Task | None: ...

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
        self, task_id: str, status: str, *, writer_token: str | None = None
    ) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # slice 3 adds the CAS reject on current_run_id != writer_token (audit C2);
        # slice 1 records the claim token (first writer claims) without rejecting.
        if writer_token is not None and task.current_run_id is None:
            task.current_run_id = writer_token
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

    async def archive(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.status = TaskState.ARCHIVED
        task.updated_at = _now_iso()
        return task

    async def abort(self, task_id: str, reason: str | None = None) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # audit C1: the real abort is cancel_inflight → await_quiescent → terminal
        # (+ seq-fence), so no in-flight write lands after the terminal transition.
        # That needs Session access (slice 3); the stub sets the terminal state only.
        task.status = TaskState.ABORTED
        task.updated_at = _now_iso()
        return task

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
