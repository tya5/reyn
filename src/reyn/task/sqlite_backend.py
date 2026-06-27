"""Sqlite Task backend (#1953 slice 2) — the first durable backend.

Implements the ``TaskBackend`` contract against a single sqlite database.

Concurrency (part-4 Option A, lead-confirmed):
  - **one persistent connection** + an in-loop ``asyncio.Lock`` serializing all
    access (mimics ``state_log.py`` / ``agent_locks.py``). Short indexed
    statements are sub-millisecond and non-yielding, so sync-on-loop is fine.
  - ``PRAGMA journal_mode=WAL`` + ``PRAGMA busy_timeout=0`` (non-zero is a
    loop-block trap; with the in-process lock there is no in-process contention,
    and cross-process contention should fail fast, not block the event loop).
  - writes open an explicit ``BEGIN IMMEDIATE`` transaction (WAL writer-upgrade
    deadlock avoidance — reyn's first use, so explicit).

Single-writer is a **fixed-equality CAS on ``assignee``** (the settled model,
#1953): a Task is owned by its **assignee session** (the #1814 per-contextId
routing-key) and ``assignee`` is immutable, so ``update_status`` succeeds only
when ``assignee == caller_session_id`` (``OpContext.session_id``); otherwise
``rowcount == 0`` → the write is rejected. No claim token / version is needed
(the key is fixed at create) — the backend ``asyncio.Lock`` serialises writers
and the immutable assignee key makes a single writer structural.

We deliberately do NOT copy ``SqliteIndexBackend``'s per-op fresh-connection +
unguarded blocking ``conn.execute`` (PR-N7 on-loop-blocking hazard); only its
structure (WAL PRAGMA, per-DB file, sandbox-gated path) is the reference.

Task state lives here in ``task_events`` (the backend is the source of truth);
the WAL ``state_log`` closed kind-vocabulary is NOT expanded (P7). The P6 audit
hook (``EventLog.emit``) is wired at the op/handler layer in slice 3.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from reyn.task.backend import find_cycle_path
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
)

# Terminal status values (no further transitions) — used by the update_status
# terminal-guard (the abort straggler-reject, #1953 Option B).
_TERMINAL_VALUES: tuple[str, ...] = tuple(s.value for s in TERMINAL_STATES)
_TERMINAL_PLACEHOLDERS = ",".join("?" * len(_TERMINAL_VALUES))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  -- #2187 backend-master: the task BINDING (assignee/requester/requester_kind)
  -- lives in the WAL-derived SubscriptionRegistry (the binding authority), not
  -- here. The backend stores CONTENT + the dependency DAG + task STATE only and
  -- reads the binding THROUGH the injected subscription_reader. (No binding
  -- columns — _migrate_columns drops them from pre-2c-iii dbs.)
  origin TEXT NOT NULL,
  status TEXT NOT NULL,
  description TEXT,
  created_by TEXT,
  awaiting_since REAL,
  unblock_predicate TEXT,
  tools TEXT,
  result TEXT,
  -- #2187: soft-delete retention marker, orthogonal to the lifecycle `status`
  -- (set alongside ABORTED by abort(); the list hidden-filter keys on it).
  archived_at TEXT,
  -- #2187 §3.5: this child's decomposition-link type to its parent (awaited /
  -- background). CONTENT (like deps), marked at create. NULL on a pre-5b row →
  -- the AWAITED default in _row_to_task.
  link_type TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_links(
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY(task_id, depends_on)
);
-- #1953 slice 6 (OQ-8): reverse (dependents) lookup for readiness recompute.
CREATE INDEX IF NOT EXISTS idx_task_links_depends_on ON task_links(depends_on);
CREATE TABLE IF NOT EXISTS task_runs(
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  status TEXT,
  started_at TEXT,
  ended_at TEXT,
  cost REAL NOT NULL DEFAULT 0,
  last_heartbeat_at TEXT
);
CREATE TABLE IF NOT EXISTS task_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS task_comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  author TEXT,
  ts TEXT NOT NULL,
  body TEXT
);
"""

_TASK_COLUMNS = (
    "task_id, name, origin, status, description, "
    "created_by, awaiting_since, tools, result, archived_at, link_type, created_at, updated_at"
)


class SqliteTaskBackend:
    """Durable Task backend backed by a single sqlite database.

    The Task backend is the EXTERNAL MASTER of task-state (#2187): it is NOT
    rewound by time-travel — Reyn rewinds only its internal trajectory (the
    runtime snapshot + workspace substrates), never the external task tracker.
    The dep graph + decision-ops stay WAL-durable in the live db, independent of
    any rewind."""

    def __init__(self, db_path: str | Path, *, subscription_reader=None) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open(init_schema=True)
        self._lock = asyncio.Lock()
        # #2187 backend-master: the WAL-derived SUBSCRIPTION reader (the binding
        # authority — assignee/requester/requester_kind). The backend holds
        # task-STATE; the binding is hydrated THROUGH this reader. None =
        # direct/test construction (the stored columns stand, the additive 2c-i
        # fallback).
        self._subscription_reader = subscription_reader

    def _hydrate_binding(self, task: "Task | None") -> "Task | None":
        """#2187 backend-master (2c-i): overlay the WAL-derived binding
        (assignee/requester/requester_kind) onto a Task reconstructed from a row
        (the read-through). Additive — overlays ONLY a field the reader HAS a
        record for, so the stored-column value is the fallback. No-op when there
        is no reader."""
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

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Apply all additive column migrations to ``tasks`` that ``CREATE TABLE IF
        NOT EXISTS`` cannot handle on its own (it does not add missing columns to an
        already-existing table).  Safe no-op on a current-schema table: the
        ``PRAGMA table_info`` guard skips any column that already exists.

        Called from ``_open(init_schema=True)`` (first open / schema init), so a
        pre-existing DB is brought to the current schema — robust to additive
        schema evolution by construction."""
        existing = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        # #1953 slice P2: additive migration for DBs created before tools / result.
        # #2187: archived_at (the soft-delete retention marker) for pre-#2187 DBs.
        for col in ("tools", "result", "archived_at", "link_type"):
            if col not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        # #2187 backend-master (2c-iii): the task BINDING moved to the WAL-derived
        # SubscriptionRegistry (the binding authority). Drop the now-unused binding
        # columns from a pre-2c-iii db — they are NOT NULL, so a binding-less INSERT
        # would violate the constraint. SQLite ≥ 3.35 DROP COLUMN; the columns are
        # not indexed/constrained so the drop is clean. The WAL subscription replay
        # is the source for the existing rows' binding (recovery == live).
        for col in ("assignee", "requester", "requester_kind"):
            if col in existing:
                conn.execute(f"ALTER TABLE tasks DROP COLUMN {col}")
        conn.commit()

    def _open(self, *, init_schema: bool = False) -> sqlite3.Connection:
        """Open the live connection with the backend's fixed pragmas.
        ``init_schema=True`` on first open: creates tables (``CREATE TABLE IF NOT
        EXISTS`` is idempotent) and runs ``_migrate_columns`` to bring any
        pre-existing DB to the current schema."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=0")
        if init_schema:
            conn.executescript(_SCHEMA)
            self._migrate_columns(conn)
        return conn

    def close(self) -> None:
        self._conn.close()

    # ── helpers ─────────────────────────────────────────────────────────────

    def _emit(self, task_id: str, kind: str, **payload) -> None:
        """Write a task_events row (the backend's own audit projection)."""
        self._conn.execute(
            "INSERT INTO task_events(task_id, ts, kind, payload) VALUES (?,?,?,?)",
            (task_id, _now_iso(), kind, json.dumps(payload) if payload else None),
        )

    def _deps(self, task_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT depends_on FROM task_links WHERE task_id=? ORDER BY depends_on",
            (task_id,),
        ).fetchall()
        return [r["depends_on"] for r in rows]

    def _exists(self, task_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone() is not None

    def _validate_edge(self, task_id: str, depends_on: str) -> None:
        """Shared edge guard (#1953 slice 6, OQ-1/OQ-4): ``depends_on`` must exist
        and the edge must not create a cycle. Called by both ``create(deps)`` and
        ``add_dependency`` (completeness-by-construction). Read-only — call inside
        the lock, before opening the write transaction."""
        if not self._exists(depends_on):
            raise TaskDepNotFoundError(task_id, depends_on)
        path = find_cycle_path(self._deps, task_id, depends_on)
        if path is not None:
            raise TaskCycleError(task_id, depends_on, path)

    def _all_deps_completed(self, task_id: str) -> bool:
        # No deps → vacuously satisfied (the slice 6-ext I-1 case). Else every dep
        # must exist + be completed.
        deps = self._deps(task_id)
        for d in deps:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (d,)
            ).fetchone()
            if row is None or row["status"] != TaskState.DONE.value:
                return False
        return True

    def _derive_readiness(self, task_id: str, *, allow_demote: bool = False) -> str | None:
        """OS-authority readiness derivation (#1953 slice 6-ext, P3 — no CAS) — the
        shared primitive for recompute / remove / repoint. Sync; call INSIDE the
        caller's ``BEGIN IMMEDIATE`` transaction. Pre-run states only
        {pending, ready, blocked}: always promote ``blocked → ready`` when all deps
        are satisfied; only when ``allow_demote`` re-block {pending, ready} →
        ``blocked`` when not (recompute / remove relax → promote-only; repoint
        re-wires → full). IN_PROGRESS / terminal left untouched (assignee owns the
        run). Returns the transition (``"ready"`` / ``"blocked"``) or None."""
        row = self._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        status = row["status"]
        if status not in (TaskState.READY.value, TaskState.BLOCKED.value):
            return None
        satisfied = self._all_deps_completed(task_id)
        if satisfied and status == TaskState.BLOCKED.value:
            new = TaskState.READY.value
        elif allow_demote and not satisfied and status == TaskState.READY.value:
            new = TaskState.BLOCKED.value
        else:
            return None
        self._conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (new, _now_iso(), task_id),
        )
        self._emit(task_id, "readiness_changed", to=new)
        return new

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        # #2187 backend-master (2c-iii): no binding columns — the Task is
        # reconstructed with a PLACEHOLDER binding (empty assignee/requester,
        # default SESSION kind); callers overlay the WAL-derived binding (the
        # authority) via _hydrate_binding. _row_to_task itself stays pure
        # content + DAG + state.
        return Task(
            task_id=row["task_id"],
            name=row["name"],
            assignee="",
            requester="",
            requester_kind=TaskRequesterKind.SESSION,
            link_type=TaskLinkType(row["link_type"]) if row["link_type"] else TaskLinkType.AWAITED,
            origin=TaskOrigin(row["origin"]),
            status=TaskState(row["status"]),
            description=row["description"],
            created_by=row["created_by"],
            awaiting_since=row["awaiting_since"],
            archived_at=row["archived_at"],
            deps=self._deps(row["task_id"]),
            tools=json.loads(row["tools"]) if row["tools"] else [],
            result=row["result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _fetch(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._hydrate_binding(self._row_to_task(row)) if row is not None else None

    def _children_of(self, pid: str) -> list[Task]:
        """Direct decomposition children of ``pid`` — IN-LOCK, no re-lock (the
        shared primitive the in-lock abort cascade and the public ``children_of``
        both build on; the public method adds the lock). The collision-safe
        ownership filter (#2187 5b, finding D): the candidate set is the
        WAL-subscription's task ids (the binding authority — no binding columns),
        kept only when ``requester==pid AND requester_kind=="task"``; the ``task``
        marker is REQUIRED because a session routing-key uuid can collide with a
        task-id uuid. No reader (direct/test construction) → no owned children."""
        reader = self._subscription_reader
        if reader is None:
            return []
        out: list[Task] = []
        for tid in reader.task_ids():
            if (reader.requester_of(tid) == pid
                    and reader.requester_kind_of(tid) == "task"):
                t = self._fetch(tid)
                if t is not None:
                    out.append(t)
        return out

    # ── TaskBackend contract ────────────────────────────────────────────────

    async def create(self, task: Task) -> Task:
        async with self._lock:
            # Validate every born-with dep (existence + cycle) before writing
            # (OQ-4 shared helper on the create path); read-only, pre-transaction.
            for dep in task.deps:
                self._validate_edge(task.task_id, dep)
            # Born-blocked (OQ-3 origin of `blocked`): a task born with deps not ALL
            # completed is OS-derived `blocked` at birth (§13) — initial-status
            # derivation, not a post-hoc flip (OQ-2). Deps-less tasks (the A2A create
            # path) keep their requested status.
            status = task.status
            if task.deps:
                dep_statuses = [
                    (self._conn.execute(
                        "SELECT status FROM tasks WHERE task_id=?", (dep,)
                    ).fetchone() or {"status": None})["status"]
                    for dep in task.deps
                ]
                if not all(s == TaskState.DONE.value for s in dep_statuses):
                    status = TaskState.BLOCKED
            task.status = status
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"INSERT INTO tasks({_TASK_COLUMNS}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id, task.name, task.origin.value, task.status.value,
                    task.description, task.created_by, task.awaiting_since,
                    json.dumps(task.tools) if task.tools else None, task.result,
                    task.archived_at, task.link_type.value, task.created_at, task.updated_at,
                ),
            )
            for dep in task.deps:
                self._conn.execute(
                    "INSERT OR IGNORE INTO task_links(task_id, depends_on) VALUES (?,?)",
                    (task.task_id, dep),
                )
            self._emit(task.task_id, "created", assignee=task.assignee, status=task.status.value)
            self._conn.commit()
            return task

    async def get(self, task_id: str) -> Task | None:
        async with self._lock:
            return self._fetch(task_id)

    async def list(
        self,
        *,
        assignee: str | None = None,
        requester: str | None = None,
        status: str | None = None,
    ) -> list[Task]:
        # #2187 backend-master (2c-iii): the binding (assignee/requester) is no
        # longer a column, so it cannot be an SQL WHERE clause — only the
        # backend-owned ``status`` stays SQL. The binding filters are applied
        # POST-fetch against the hydrated binding (the WAL-derived authority).
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks{where} ORDER BY created_at, task_id",
                params,
            ).fetchall()
            tasks = [self._hydrate_binding(self._row_to_task(r)) for r in rows]
        if assignee is not None:
            tasks = [t for t in tasks if t.assignee == assignee]
        if requester is not None:
            tasks = [t for t in tasks if t.requester == requester]
        return tasks

    async def update_status(
        self, task_id: str, status: str, *, caller_session_id: str | None = None
    ) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            # #2187 backend-master: the backend is the task-state MASTER — it APPLIES the
            # status request and enforces only the STATE-VALIDITY rule: no transition OUT
            # of a terminal state (the abort straggler-reject, Option B #1953). The
            # single-writer OWNERSHIP CAS (caller == assignee) is now OP-LAYER gating
            # against the WAL-subscription (the binding lives in the WAL, not here);
            # ``caller_session_id`` is recorded in the audit projection but NOT gated on.
            # rowcount == 0 → no such task, or a terminal task.
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=? "
                f"WHERE task_id=? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})",
                (status, _now_iso(), task_id, *_TERMINAL_VALUES),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT status FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    return None
                raise PermissionError(
                    f"task {task_id!r} status-write rejected: task is terminal "
                    f"({row['status']}) — no further transitions (e.g. post-abort straggler)"
                )
            self._emit(task_id, "status_changed", status=status, by=caller_session_id)
            self._conn.commit()
            return self._fetch(task_id)

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None:
        async with self._lock:
            if not self._exists(task_id):
                return None
            # Pure topology write (OQ-2): validate (existence + cycle) then record
            # the edge — never flips this task's status (readiness is OS-derived).
            self._validate_edge(task_id, depends_on)
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR IGNORE INTO task_links(task_id, depends_on) VALUES (?,?)",
                (task_id, depends_on),
            )
            self._conn.execute(
                "UPDATE tasks SET updated_at=? WHERE task_id=?", (_now_iso(), task_id)
            )
            self._emit(task_id, "dependency_added", depends_on=depends_on)
            self._conn.commit()
            return self._fetch(task_id)

    async def dependents(self, task_id: str) -> list[Task]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT task_id FROM task_links WHERE depends_on=?", (task_id,)
            ).fetchall()
            return [t for t in (self._fetch(r["task_id"]) for r in rows) if t is not None]

    async def remove_dependency(self, task_id: str, depends_on: str) -> Task | None:
        async with self._lock:
            if not self._exists(task_id):
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Idempotent: DELETE is a no-op on a missing edge. Dropping an edge
                # only RELAXES → the re-derive can only promote (incl. I-1).
                self._conn.execute(
                    "DELETE FROM task_links WHERE task_id=? AND depends_on=?",
                    (task_id, depends_on),
                )
                self._conn.execute(
                    "UPDATE tasks SET updated_at=? WHERE task_id=?", (_now_iso(), task_id)
                )
                self._emit(task_id, "dependency_removed", depends_on=depends_on)
                self._derive_readiness(task_id)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._fetch(task_id)

    async def repoint_dependency(
        self, task_id: str, from_depends_on: str, to_depends_on: str
    ) -> Task | None:
        async with self._lock:
            if not self._exists(task_id):
                return None
            # Cycle-check the NEW edge BEFORE any mutation (read-only, pre-tx): a
            # cycle/dangling repoint raises → nothing changes (atomic). Safe with the
            # from-edge present (task→from is task's outgoing, never on a to→…→task path).
            self._validate_edge(task_id, to_depends_on)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM task_links WHERE task_id=? AND depends_on=?",
                    (task_id, from_depends_on),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO task_links(task_id, depends_on) VALUES (?,?)",
                    (task_id, to_depends_on),
                )
                self._conn.execute(
                    "UPDATE tasks SET updated_at=? WHERE task_id=?", (_now_iso(), task_id)
                )
                self._emit(task_id, "dependency_repointed",
                           from_depends_on=from_depends_on, to_depends_on=to_depends_on)
                # Re-block then re-evaluate (may demote on the new edge or promote).
                self._derive_readiness(task_id, allow_demote=True)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._fetch(task_id)

    async def recompute_readiness(self, completed_task_id: str) -> list[Task]:
        """OS-authority readiness recompute (#1953 slice 6, OQ-3): no
        ``caller_session_id`` / no CAS (P3 scheduling, like ``abort``). Re-derive
        readiness for each dependent of the just-completed task via the shared
        ``_derive_readiness`` primitive — a completion only RELAXES, so only a
        promote (``blocked → ready``) can fire here."""
        async with self._lock:
            dependents = [
                r["task_id"] for r in self._conn.execute(
                    "SELECT task_id FROM task_links WHERE depends_on=?", (completed_task_id,)
                ).fetchall()
            ]
            promoted_ids: list[str] = []
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for dep_task in dependents:
                    if self._derive_readiness(dep_task) == "ready":
                        promoted_ids.append(dep_task)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return [t for t in (self._fetch(tid) for tid in promoted_ids) if t is not None]

    async def abort(self, task_id: str, reason: str | None = None) -> list[Task]:
        """abort = delete (cooperative-terminal, #1953 Option B): set this task +
        its whole sub-tree to ``archived`` (DOWN-cascade, §18). There is no forced
        cancel — the assignee's in-flight work discovers the abort at its next
        status-write, which the terminal state rejects (so no straggler lands; and
        a sibling task's work is untouched). Returns the aborted tasks (root
        first) so the caller can emit a disposition event per task (UP-notify,
        2b-2); ``[]`` if no such task."""
        async with self._lock:
            origin_row = self._conn.execute(
                "SELECT origin FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if origin_row is None:
                return []
            root_external = origin_row["origin"] == TaskOrigin.EXTERNAL.value
            # BFS the down-cascade closure: this task + every OWNED descendant
            # (§16/§18 ownership forest — a task-as-request owns its sub-tasks). An
            # edge child→pid is followed when requester=pid AND requester_kind='task'.
            # The ``requester_kind='task'`` guard is REQUIRED: a session routing-key
            # can collide with a task-id uuid, so a bare requester=pid would wrongly
            # cascade a session-requester task; the marker disambiguates →
            # collision-safe. Acyclic (one requester/task → earlier task) + the in-set
            # guard → bounded. In-lock BFS (NOT recursive — the lock is not re-entrant).
            # (The legacy parent_id decomposition tree was removed in §16 slice C —
            # the requester edge is the sole decomposition relation.)
            #
            # #2187 backend-master (2c-iii): the requester edge is the WAL-derived
            # SUBSCRIPTION binding, not a column — the closure walks ``_children_of``
            # (the in-lock, no-relock shared decomposition primitive that applies the
            # collision-safe ``requester_kind=='task'`` guard EXACTLY — finding D, 5b).
            # No reader (direct/test) → no owned children → the cascade aborts the root.
            to_abort: list[str] = [task_id]
            frontier = [task_id]
            while frontier:
                pid = frontier.pop()
                for child in self._children_of(pid):
                    if child.task_id not in to_abort:
                        to_abort.append(child.task_id)
                        frontier.append(child.task_id)
            # #2107 S2 (origin-split): an EXTERNAL terminal's dep-DAG DEPENDENTS can't be
            # recovered — the external requester gives up (no in-session re-wire; that is
            # the SELF path's requester wake, §16 S1). Abort them too, transitively (a dep
            # graph is single-origin, so they are all EXTERNAL); the webhook sweep then
            # propagates each archived dependent to the A2A client. The cascade lives in
            # this ONE seam so every abort caller gets it by construction (the A2A cancel
            # endpoint + /tasks kill abort the backend directly). Done as an in-lock BFS
            # (NOT a recursive self.abort — the lock is not re-entrant); the non-terminal
            # filter + the in-set guard bound it (no cycle / double-abort).
            if root_external:
                dep_frontier = list(to_abort)
                while dep_frontier:
                    tid = dep_frontier.pop()
                    for row in self._conn.execute(
                        "SELECT task_id FROM task_links WHERE depends_on=?", (tid,)
                    ).fetchall():
                        dep = row["task_id"]
                        if dep in to_abort:
                            continue
                        srow = self._conn.execute(
                            "SELECT status FROM tasks WHERE task_id=?", (dep,)
                        ).fetchone()
                        if srow is not None and srow["status"] not in _TERMINAL_VALUES:
                            to_abort.append(dep)
                            dep_frontier.append(dep)
            self._conn.execute("BEGIN IMMEDIATE")
            now = _now_iso()
            for tid in to_abort:
                # #2187: abort sets BOTH the ABORTED lifecycle state and archived_at
                # (the orthogonal soft-delete retention marker — preserves the
                # hidden-from-list UX of the old ARCHIVED state).
                self._conn.execute(
                    "UPDATE tasks SET status=?, archived_at=?, updated_at=? WHERE task_id=?",
                    (TaskState.ABORTED.value, now, now, tid),
                )
                self._emit(tid, "aborted", root=task_id)
            self._conn.commit()
            return [t for t in (self._fetch(tid) for tid in to_abort) if t is not None]

    async def children_of(self, pid: str) -> list[Task]:
        """Direct decomposition children of ``pid`` (#2187 5b) — the public,
        lock-acquiring wrapper over the in-lock ``_children_of`` primitive."""
        async with self._lock:
            return self._children_of(pid)

    async def open_child_counts(self, pid: str) -> ChildCounts:
        """Open (non-terminal) child counts of ``pid`` split by link type (#2187
        §3.4) — derived on-demand from the children's durable states. The
        completion-join and waker reconciler read it."""
        async with self._lock:
            awaited = background = 0
            for child in self._children_of(pid):
                if child.status in TERMINAL_STATES:
                    continue
                if child.link_type is TaskLinkType.BACKGROUND:
                    background += 1
                else:
                    awaited += 1
            return ChildCounts(awaited=awaited, background=background)

    async def set_result(self, task_id: str, result: str) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE tasks SET result=?, updated_at=? WHERE task_id=?",
                (result, _now_iso(), task_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                return None
            self._emit(task_id, "result_set")
            self._conn.commit()
            return self._fetch(task_id)

    async def set_awaiting(self, task_id: str, awaiting_since: float | None) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE tasks SET awaiting_since=?, updated_at=? WHERE task_id=?",
                (awaiting_since, _now_iso(), task_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                return None
            self._emit(task_id, "awaiting_set", awaiting_since=awaiting_since)
            self._conn.commit()
            return self._fetch(task_id)

    async def set_unblock_predicate(self, task_id: str, predicate: str) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE tasks SET unblock_predicate=?, updated_at=? WHERE task_id=?",
                (predicate, _now_iso(), task_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                return None
            self._emit(task_id, "predicate_registered")
            self._conn.commit()
            return self._fetch(task_id)

    async def add_comment(self, task_id: str, author: str, body: str) -> str | None:
        async with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone() is None:
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "INSERT INTO task_comments(task_id, author, ts, body) VALUES (?,?,?,?)",
                (task_id, author, _now_iso(), body),
            )
            self._emit(task_id, "comment_added", author=author)
            self._conn.commit()
            return f"c{cur.lastrowid}"

    # ── introspection (test / slice-3 events wiring) ────────────────────────

    async def events(self, task_id: str) -> list[dict]:
        """Return the backend's own task_events rows (audit projection)."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, payload FROM task_events WHERE task_id=? ORDER BY seq",
                (task_id,),
            ).fetchall()
            return [
                {"seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]) if r["payload"] else None}
                for r in rows
            ]
