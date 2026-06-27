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
    TaskOrigin,
    TaskRequesterKind,
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

    async def set_result(self, task_id: str, result: str) -> Task | None:
        """Record the exec-layer output text on the task (#1953 slice P2). The
        exec-engine writes it on a unit's completion; a dependent reads its deps'
        results from here. None for an unknown task."""
        ...

    async def abort(self, task_id: str, reason: str | None = None) -> list[Task]: ...

    async def set_awaiting(self, task_id: str, awaiting_since: float | None) -> Task | None: ...

    async def set_unblock_predicate(self, task_id: str, predicate: str) -> Task | None: ...

    async def add_comment(self, task_id: str, author: str, body: str) -> str | None: ...


class InMemoryTaskBackend:
    """Process-local dict-backed backend (slice 1 stub + the ``in-memory``
    config option for tests). Not durable, not crash-safe — sqlite (slice 2)
    is the first durable backend. Single-threaded async; no locking needed
    because slice 1 does not yet enforce CAS."""

    def __init__(self, *, subscription_reader=None) -> None:
        self._tasks: dict[str, Task] = {}
        # slice-1 stub stores for ops whose enforcement lands later.
        self._predicates: dict[str, str] = {}
        self._comments: dict[str, list[dict]] = {}
        self._comment_seq = 0
        # #2187 backend-master: the WAL-derived SUBSCRIPTION reader (the binding
        # authority — assignee/requester/requester_kind). The backend holds
        # task-STATE; the binding is hydrated THROUGH this reader. None =
        # direct/test construction (the stored columns stand, the additive 2c-i
        # fallback).
        self._subscription_reader = subscription_reader

    def _hydrate_binding(self, task: "Task | None") -> "Task | None":
        """#2187 backend-master (2c-i): overlay the WAL-derived binding
        (assignee/requester/requester_kind) onto a Task before returning it (the
        read-through). Additive — overlays ONLY a field the reader HAS a record
        for, so the stored-column value is the fallback. No-op when there is no
        reader."""
        if task is not None and self._subscription_reader is not None:
            a = self._subscription_reader.assignee_of(task.task_id)
            if a is not None:
                task.assignee = a
            r = self._subscription_reader.requester_of(task.task_id)
            if r is not None:
                task.requester = r
            rk = self._subscription_reader.requester_kind_of(task.task_id)
            if rk is not None:
                task.requester_kind = TaskRequesterKind(rk)
        return task

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
            self._tasks[d].status == TaskState.DONE for d in task.deps
        ):
            task.status = TaskState.BLOCKED
        self._tasks[task.task_id] = task
        return task

    async def get(self, task_id: str) -> Task | None:
        return self._hydrate_binding(self._tasks.get(task_id))

    async def list(
        self,
        *,
        assignee: str | None = None,
        requester: str | None = None,
        status: str | None = None,
    ) -> list[Task]:
        out = [self._hydrate_binding(t) for t in self._tasks.values()]
        if assignee is not None:
            out = [t for t in out if t.assignee == assignee]
        if requester is not None:
            out = [t for t in out if t.requester == requester]
        if status is not None:
            out = [t for t in out if t.status.value == status]
        return out

    async def update_status(
        self, task_id: str, status: str, *, caller_session_id: str | None = None
    ) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # #2187 backend-master: the backend is the task-state MASTER — it APPLIES the
        # request + enforces only the STATE-VALIDITY terminal-guard below. The
        # single-writer OWNERSHIP CAS (caller == assignee) is now OP-LAYER gating against
        # the WAL-subscription; ``caller_session_id`` is accepted (audit) but not gated.
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
            d in self._tasks and self._tasks[d].status == TaskState.DONE
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
        if task.status not in (TaskState.READY, TaskState.BLOCKED):
            return None
        satisfied = self._all_deps_satisfied(task)
        if satisfied and task.status is TaskState.BLOCKED:
            task.status = TaskState.READY
            task.updated_at = _now_iso()
            return "ready"
        if allow_demote and not satisfied and task.status is TaskState.READY:
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
        root_origin = self._tasks[task_id].origin
        # DOWN-cascade closure: archive this task + every OWNED descendant (§16/§18
        # ownership forest — a task-as-request owns its sub-tasks). An edge child→pid
        # is followed when requester==pid AND requester_kind==TASK. The
        # ``requester_kind==TASK`` guard is REQUIRED: a session routing-key
        # (spawned-session uuid) can collide with a task-id uuid, so a bare
        # requester==pid would wrongly cascade a session-requester task; the marker
        # disambiguates → collision-safe. Acyclic (one requester/task, set at create
        # to an earlier task) + the in-set guard → bounded, no double-abort. (The
        # legacy parent_id decomposition tree was removed in §16 slice C — the
        # requester edge is the sole decomposition relation.)
        #
        # #2187 backend-master (2c-iii): the requester binding is now the WAL-derived
        # SUBSCRIPTION authority — the cascade PREFERS the reader (requester_of /
        # requester_kind_of) when one is wired; the stored Task fields are the
        # no-reader fallback (direct/test construction). The ``requester_kind=='task'``
        # marker guard is preserved EXACTLY in both paths.
        reader = self._subscription_reader
        subtree: list[str] = [task_id]
        frontier = [task_id]
        while frontier:
            pid = frontier.pop()
            for tid, t in self._tasks.items():
                if reader is not None:
                    owned = (reader.requester_of(tid) == pid
                             and reader.requester_kind_of(tid) == "task")
                else:
                    owned = (t.requester == pid
                             and t.requester_kind is TaskRequesterKind.TASK)
                if owned and tid not in subtree:
                    subtree.append(tid)
                    frontier.append(tid)
        aborted: list[Task] = []
        now = _now_iso()
        for tid in subtree:
            self._tasks[tid].status = TaskState.ABORTED
            # #2187: soft-delete is the orthogonal retention dimension (archived_at),
            # not a state — abort sets BOTH the ABORTED lifecycle state and the
            # retention marker (preserves the hidden-from-list UX of the old ARCHIVED).
            self._tasks[tid].archived_at = now
            self._tasks[tid].updated_at = now
            aborted.append(self._tasks[tid])
        # #2107 S2 (origin-split): an EXTERNAL terminal's dep-DAG DEPENDENTS can't be
        # recovered — the external requester gives up (no in-session re-wire; that is the
        # SELF path's requester wake, §16 S1). So abort them too, transitively: each
        # dependent's own abort recurses for ITS dependents (a dep graph is single-origin
        # — a dependency lives within one decomposition — so they are all EXTERNAL). The
        # webhook sweep then propagates every archived dependent to the A2A client. The
        # cascade lives HERE (one seam) so EVERY abort caller gets it by construction (the
        # A2A cancel endpoint + /tasks kill abort the backend directly, bypassing the op
        # layer). Bounded by the terminal-state idempotence (an already-archived task is
        # skipped → no cycle / double-abort).
        if root_origin is TaskOrigin.EXTERNAL:
            for t in list(aborted):
                for dep in await self.dependents(t.task_id):
                    if dep.status not in TERMINAL_STATES:
                        aborted.extend(await self.abort(dep.task_id, reason=reason))
        return aborted

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
