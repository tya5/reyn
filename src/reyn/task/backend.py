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

from typing import Callable, Protocol

from reyn.task.model import (
    TERMINAL_STATES,
    Task,
    TaskCycleError,
    TaskDepNotFoundError,
    TaskState,
    _now_iso,
)


def find_cycle_path(
    deps_of: Callable[[str], list[str]], task_id: str, depends_on: str
) -> list[str] | None:
    """Return the cycle node-path if adding the edge ``task_id depends-on
    depends_on`` would create a cycle in the dependency DAG, else ``None``
    (#1953 slice 6, OQ-4). A cycle forms iff ``task_id`` is already reachable
    from ``depends_on`` by following depends-on edges (then ``task_id ->
    depends_on -> ... -> task_id``), or the edge is a self-loop.

    Pure + backend-agnostic: ``deps_of(node)`` returns ``node``'s depends-on
    ids, so the in-memory and sqlite backends share one cycle check (the
    shared-helper completeness OQ-4 calls for). Returns the offending cycle as a
    node sequence for a decision-enabling error (OQ-5)."""
    if task_id == depends_on:
        return [task_id, depends_on]
    parent: dict[str, str | None] = {depends_on: None}
    stack: list[str] = [depends_on]
    while stack:
        node = stack.pop()
        for d in deps_of(node):
            if d == task_id:
                # Reconstruct depends_on .. node, then close the cycle T -> .. -> T.
                chain = [node]
                while parent[chain[-1]] is not None:
                    chain.append(parent[chain[-1]])  # type: ignore[arg-type]
                chain.reverse()
                return [task_id, *chain, task_id]
            if d not in parent:
                parent[d] = node
                stack.append(d)
    return None


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

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None:
        """Add a depends-on edge (pure topology write, OQ-2 — never flips the
        task's status). Validates ``depends_on`` exists (raises
        ``TaskDepNotFoundError``) and that the edge does not create a cycle
        (raises ``TaskCycleError``); the op layer maps both to error results.
        Returns None for an unknown ``task_id``."""
        ...

    async def recompute_readiness(self, completed_task_id: str) -> list[Task]:
        """OS-authority readiness recompute (#1953 slice 6, OQ-3): a predecessor
        reaching ``completed`` → re-evaluate its dependents → any whose deps are
        ALL ``completed`` transitions ``blocked → ready``. This is an OS
        scheduling write (P3), **not** an assignee progress write — it takes no
        ``caller_session_id`` and bypasses the single-writer CAS (like ``abort``).
        Returns the newly-readied tasks (so the caller emits P6 + later nudges)."""
        ...

    async def dependents(self, task_id: str) -> list[Task]:
        """Tasks that depend ON ``task_id`` (reverse edge lookup, §13) — the
        abort/failed → parent-routing hub reads it to find stuck dependents."""
        ...

    async def remove_dependency(self, task_id: str, depends_on: str) -> Task | None:
        """Drop a depends-on edge (#1953 slice 6-ext) — requester topology write,
        idempotent (no-op on a missing edge). Dropping an edge only RELAXES, so the
        OS-authority re-derive may promote a now-satisfied ``blocked`` task (incl.
        the I-1 last-dep-removed → ready case), never demote. None for unknown task."""
        ...

    async def repoint_dependency(
        self, task_id: str, from_depends_on: str, to_depends_on: str
    ) -> Task | None:
        """Atomically repoint an edge ``from_depends_on`` → ``to_depends_on``
        (#1953 slice 6-ext) — the parent's primary recovery move. Cycle-checks the
        NEW edge BEFORE any mutation (raises ``TaskCycleError`` / ``TaskDepNotFoundError``
        → the op layer returns the structured error, nothing changed). Re-blocks
        then re-evaluates readiness (the new edge may demote or the graph may now be
        satisfied). None for unknown task."""
        ...

    async def record_cost(self, task_id: str, delta: float) -> Task | None:
        """Accumulate ``delta`` onto the task's ``cost_accum`` (#1953 slice 8) — the
        per-Task cost-attribution primitive. Pure write (the cap-hit check + the
        ``cap_exceeded`` disposition routing is the op layer's, which has the ctx).
        None for an unknown task."""
        ...

    async def set_result(self, task_id: str, result: str) -> Task | None:
        """Record the exec-layer output text on the task (#1953 slice P2). The
        exec-engine writes it on a unit's completion; a dependent reads its deps'
        results from here. None for an unknown task."""
        ...

    async def abort(self, task_id: str, reason: str | None = None) -> list[Task]: ...

    async def set_awaiting(self, task_id: str, awaiting_since: float | None) -> Task | None: ...

    async def set_unblock_predicate(self, task_id: str, predicate: str) -> Task | None: ...

    async def add_comment(self, task_id: str, author: str, body: str) -> str | None: ...

    # ── Rewind substrate (#1953 slice R) ─────────────────────────────────────
    # A backend opts INTO session-rewind participation by setting
    # ``supports_rewind = True`` and implementing per-generation snapshot/restore.
    # The OS captures a generation at every WAL boundary seq (``SnapshotJournal.
    # cut_generation``) and restores the nearest *active* generation <= the rewind
    # target (``_materialize_rewind``) — symmetric with ``WorkspaceVersionStore``
    # (the runtime + workspace substrates). A backend whose state cannot be
    # rewound (in-memory; external trackers like gh-issue/jira) leaves
    # ``supports_rewind = False`` and the OS skips it (opt-out).
    supports_rewind: bool

    async def snapshot_generation(self, seq: int) -> None:
        """Capture a full point-in-time generation keyed by the WAL boundary
        ``seq``. Idempotent for a given seq. No-op when ``supports_rewind`` is
        False."""
        ...

    async def restore_to_seq(self, seq: int) -> None:
        """Restore backend state to the generation captured at ``seq`` — the OS
        passes the nearest active seq <= the rewind target. No-op when
        ``supports_rewind`` is False."""
        ...

    async def generation_seqs(self) -> list[int]:
        """Seqs of all captured generations, ascending — the OS resolves the
        nearest *active* one <= the rewind target (``is_active_seq``, symmetric
        with ``WorkspaceVersionStore.seqs``). Empty when ``supports_rewind`` is
        False."""
        ...

    async def prune_generations_below(self, min_keep_seq: int) -> int:
        """Drop generations older than ``min_keep_seq`` (WAL-truncation
        piggyback, bounds storage). Returns the count removed. No-op when
        ``supports_rewind`` is False."""
        ...


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

    def _deps_of(self, node: str) -> list[str]:
        t = self._tasks.get(node)
        return list(t.deps) if t is not None else []

    def _validate_edge(self, task_id: str, depends_on: str) -> None:
        """Shared edge guard (#1953 slice 6, OQ-1/OQ-4): the ``depends_on`` task
        must exist, and the edge must not create a cycle. Called by both
        ``create(deps)`` and ``add_dependency`` (completeness-by-construction)."""
        if depends_on not in self._tasks:
            raise TaskDepNotFoundError(task_id, depends_on)
        path = find_cycle_path(self._deps_of, task_id, depends_on)
        if path is not None:
            raise TaskCycleError(task_id, depends_on, path)

    async def create(self, task: Task) -> Task:
        # Validate every born-with dep (existence + cycle) before storing (OQ-4
        # shared helper on the create path too).
        for dep in task.deps:
            self._validate_edge(task.task_id, dep)
        # Born-blocked (OQ-3 origin of `blocked`): a task born with deps that are
        # not ALL already completed is OS-derived `blocked` at birth (§13). This is
        # an initial-status derivation, not a post-hoc status flip (OQ-2) — deps-less
        # tasks (e.g. the A2A create path) keep their requested status.
        if task.deps and not all(
            self._tasks[d].status == TaskState.COMPLETED for d in task.deps
        ):
            task.status = TaskState.BLOCKED
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
        # Pure topology write (OQ-2): validate (existence + cycle) then record the
        # edge — never flips this task's status (the requester must not write the
        # assignee's status; readiness is OS-derived on completion).
        self._validate_edge(task_id, depends_on)
        if depends_on not in task.deps:
            task.deps.append(depends_on)
        task.updated_at = _now_iso()
        return task

    def _all_deps_satisfied(self, task: Task) -> bool:
        # A dep is satisfied when it exists + is completed. No deps → vacuously
        # satisfied (the #1953 slice 6-ext I-1 case: an ordering-free task is ready).
        return all(
            d in self._tasks and self._tasks[d].status == TaskState.COMPLETED
            for d in task.deps
        )

    def _derive_readiness(self, task: Task, *, allow_demote: bool = False) -> str | None:
        """OS-authority readiness derivation (#1953 slice 6-ext, P3 — no CAS).
        Operates ONLY on the pre-run scheduling states {PENDING, READY, BLOCKED};
        an IN_PROGRESS task (the assignee owns the run) or a terminal task is left
        untouched (the single-writer split). Always promotes BLOCKED → READY when
        all deps are satisfied; **only when ``allow_demote``** re-blocks
        {PENDING, READY} → BLOCKED when they are not. Returns the transition
        (``"ready"`` / ``"blocked"``) when the status changed, else None.

        The shared primitive: completion-recompute + ``remove`` only RELAX the
        graph → promote-only (``allow_demote=False``, consistent with the OQ-2
        pure-topology rule that a mere edge change does not re-block a non-blocked
        task); ``repoint`` is a material re-wire → full re-derive (``allow_demote=True``)."""
        if task.status not in (TaskState.PENDING, TaskState.READY, TaskState.BLOCKED):
            return None
        satisfied = self._all_deps_satisfied(task)
        if satisfied and task.status is TaskState.BLOCKED:
            task.status = TaskState.READY
            task.updated_at = _now_iso()
            return "ready"
        if allow_demote and not satisfied and task.status in (TaskState.PENDING, TaskState.READY):
            task.status = TaskState.BLOCKED
            task.updated_at = _now_iso()
            return "blocked"
        return None

    async def dependents(self, task_id: str) -> list[Task]:
        """Tasks that depend ON ``task_id`` (reverse edge lookup, §13)."""
        return [t for t in self._tasks.values() if task_id in t.deps]

    async def recompute_readiness(self, completed_task_id: str) -> list[Task]:
        # OS-authority (OQ-3): re-derive readiness for each dependent of the just-
        # completed task via the shared primitive. A completion only RELAXES, so
        # the only transition that can fire here is a promote (blocked → ready).
        promoted: list[Task] = []
        for t in self._tasks.values():
            if completed_task_id not in t.deps:
                continue
            if self._derive_readiness(t) == "ready":
                promoted.append(t)
        return promoted

    async def remove_dependency(self, task_id: str, depends_on: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # Requester topology write; idempotent (no-op if the edge is absent).
        # Dropping an edge only RELAXES → the re-derive can only promote (incl. the
        # I-1 last-dep-removed → ready case), never demote.
        if depends_on in task.deps:
            task.deps.remove(depends_on)
            task.updated_at = _now_iso()
        self._derive_readiness(task)
        return task

    async def repoint_dependency(
        self, task_id: str, from_depends_on: str, to_depends_on: str
    ) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # Cycle-check the NEW edge BEFORE any mutation (atomic): a cycle/dangling
        # repoint raises → nothing changes. Safe with the from-edge still present
        # (``task → from`` is task's OUTGOING edge, never on a ``to → … → task``
        # cycle path, so no false reject).
        self._validate_edge(task_id, to_depends_on)
        if from_depends_on in task.deps:
            task.deps.remove(from_depends_on)
        if to_depends_on not in task.deps:
            task.deps.append(to_depends_on)
        task.updated_at = _now_iso()
        # Re-block then re-evaluate (the new edge may TIGHTEN → demote, or the
        # graph may now be satisfied → promote).
        self._derive_readiness(task, allow_demote=True)
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

    async def record_cost(self, task_id: str, delta: float) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.cost_accum += delta
        task.updated_at = _now_iso()
        return task

    async def set_result(self, task_id: str, result: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        task.result = result
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

    # ── Rewind substrate (#1953 slice R): opt-out ────────────────────────────
    # In-memory state is ephemeral and process-local — it cannot be rewound, so
    # this backend declares supports_rewind=False and no-ops the substrate hooks.
    supports_rewind: bool = False

    async def snapshot_generation(self, seq: int) -> None:
        return None

    async def restore_to_seq(self, seq: int) -> None:
        return None

    async def generation_seqs(self) -> list[int]:
        return []

    async def prune_generations_below(self, min_keep_seq: int) -> int:
        return 0
