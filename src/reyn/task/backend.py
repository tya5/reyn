"""Task backend contract + in-memory implementation (#1953 slice 1).

``TaskBackend`` is the **tracker-agnostic core contract** every backend must
implement (id / status / assignee / requester / links / fields). ``InMemoryTaskBackend``
is the test / ephemeral backend; the durable sqlite backend lives in
``sqlite_backend.py``. Later slices layer cascades, cycle-checks, and budget on top.

Single-writer: a Task is owned by its **assignee session** (the #1814 per-contextId
routing-key) — the single writer of ``status``. Under #2187 backend-master the assignee
is a **rebindable WAL subscription binding** (NOT an immutable field): it may be ``None``
(UNASSIGNED — the pending-assignment queue, §27-31) and changed via ``record_rebound``
(claim / owner-initiated reassign / re-queue), append-only so it stays P6/rewind-clean.
The single-writer CAS is therefore ``caller_session_id == the CURRENT (hydrated) assignee``
— a read-then-check against the live WAL binding, enforced at the OP layer (NOT a
resource-scoped permission gate, and NOT a single-run claim — a multi-turn Task spans many
runs, so no one run owns it). ``caller_session_id`` is ``OpContext.session_id``,
threaded down the runtime chain. A non-assignee write is denied at the op layer.

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
    ChildCounts,
    Task,
    TaskCycleError,
    TaskDepNotFoundError,
    TaskLinkType,
    TaskOrigin,
    TaskRequesterKind,
    TaskState,
    _now_iso,
    require_valid_status,
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
        """Transition status. The single-writer ownership CAS (caller == the task's
        CURRENT assignee) is enforced at the OP layer against the rebindable WAL
        subscription (#2187); the backend (the state master) applies the request and
        enforces only the terminal-state guard. Returns None for an unknown task."""
        ...

    async def mark_assigned(self, task_id: str) -> Task | None:
        """#2187 §27-31 pending-assignment: an UNASSIGNED task just gained an assignee
        (the binding is recorded in the WAL by the op layer) → OS-derive its now-startable
        status (READY if all deps satisfied, else BLOCKED). Idempotent / no-op for a task
        that is not UNASSIGNED (a reassigned in-flight task keeps its status). Returns the
        hydrated task (assignee reflects the just-rebound binding). None for unknown task."""
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

    async def children_of(self, pid: str) -> list[Task]:
        """Direct decomposition children of ``pid`` (#2187 5b) — the collision-safe
        ownership walk (``requester==pid AND requester_kind=="task"``, finding D),
        through the WAL-subscription binding authority. The shared primitive under
        the abort DOWN-cascade and the open-child counts."""
        ...

    async def open_child_counts(self, pid: str) -> ChildCounts:
        """Open (non-terminal) child counts of ``pid`` split by link type (#2187
        §3.4) — derived on-demand from the children's durable states. The
        completion-join and the waker reconciler read it."""
        ...

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
            if self._subscription_reader.exists(task.task_id):
                # The record is authoritative — apply its assignee even when None (an
                # explicit unbind → UNASSIGNED, #2226). A bare ``assignee_of() is None``
                # check had fallen back to the stored placeholder, so a re-queued task
                # kept reading its OLD binding. A non-existent record (direct construction
                # / no writer) keeps the stored value (the fallback).
                task.assignee = self._subscription_reader.assignee_of(task.task_id)
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
        # An UNASSIGNED task (§27-31 pending-assignment queue) is not startable
        # regardless of deps — it stays UNASSIGNED until claimed; the deps-derived
        # READY/BLOCKED status is computed at assignment (``mark_assigned``).
        if task.status is not TaskState.UNASSIGNED and task.deps and not all(
            self._tasks[d].status == TaskState.DONE for d in task.deps
        ):
            task.status = TaskState.BLOCKED
        self._tasks[task.task_id] = task
        return task

    async def mark_assigned(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.status is TaskState.UNASSIGNED:
            task.status = (
                TaskState.READY if self._all_deps_satisfied(task) else TaskState.BLOCKED
            )
            task.updated_at = _now_iso()
        return self._hydrate_binding(task)

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
        # #2187 followup data-integrity: reject an invalid status BEFORE any write — the
        # master never stores a non-member (covers paths that bypass the op Literal).
        new_status = require_valid_status(status)
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
        task.status = new_status
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
        # #2187 backend-master (2c-iii): the requester binding is the WAL-derived
        # SUBSCRIPTION authority — the closure walks ``children_of`` (which prefers
        # the reader, falls back to the stored Task fields, and applies the
        # collision-safe ``requester_kind=='task'`` guard EXACTLY — finding D, 5b).
        subtree: list[str] = [task_id]
        frontier = [task_id]
        while frontier:
            pid = frontier.pop()
            for child in await self.children_of(pid):
                if child.task_id not in subtree:
                    subtree.append(child.task_id)
                    frontier.append(child.task_id)
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

    async def children_of(self, pid: str) -> list[Task]:
        """Direct decomposition children of ``pid`` — the tasks it OWNS (#2187 5b,
        finding D). The collision-safe ownership filter: an edge child→pid is
        followed only when ``requester==pid AND requester_kind=="task"``. The
        ``task`` marker is REQUIRED — a session routing-key (spawned-session uuid)
        can collide with a task-id uuid, so a bare requester match would wrongly
        include a session-requester task. Prefers the WAL-subscription reader (the
        binding authority); the stored Task fields are the no-reader fallback
        (direct/test construction). The shared decomposition-walk primitive: the
        abort DOWN-cascade and the open-child counts both build on it."""
        reader = self._subscription_reader
        out: list[Task] = []
        for tid, t in self._tasks.items():
            if reader is not None:
                owned = (reader.requester_of(tid) == pid
                         and reader.requester_kind_of(tid) == "task")
            else:
                owned = (t.requester == pid
                         and t.requester_kind is TaskRequesterKind.TASK)
            if owned:
                out.append(t)
        return out

    async def open_child_counts(self, pid: str) -> ChildCounts:
        """Open (non-terminal) child counts of ``pid`` split by link type (#2187
        §3.4) — derived on-demand from the children's durable states, never stored.
        ``awaited`` children gate the parent's completion; ``background`` run
        parallel. The completion-join (5c) and the waker reconciler read this."""
        awaited = background = 0
        for child in await self.children_of(pid):
            if child.status in TERMINAL_STATES:
                continue
            if child.link_type is TaskLinkType.BACKGROUND:
                background += 1
            else:
                awaited += 1
        return ChildCounts(awaited=awaited, background=background)

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
