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
    Task,
    TaskCycleError,
    TaskDepNotFoundError,
    TaskOrigin,
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
  assignee TEXT NOT NULL,
  requester TEXT NOT NULL,
  origin TEXT NOT NULL,
  status TEXT NOT NULL,
  description TEXT,
  created_by TEXT,
  parent_id TEXT,
  budget_cap REAL,
  cost_accum REAL NOT NULL DEFAULT 0,
  awaiting_since REAL,
  unblock_predicate TEXT,
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
    "task_id, name, assignee, requester, origin, status, description, created_by, "
    "parent_id, budget_cap, cost_accum, awaiting_since, created_at, updated_at"
)


class SqliteTaskBackend:
    """Durable Task backend backed by a single sqlite database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=0")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

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

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            name=row["name"],
            assignee=row["assignee"],
            requester=row["requester"],
            origin=TaskOrigin(row["origin"]),
            status=TaskState(row["status"]),
            description=row["description"],
            created_by=row["created_by"],
            parent_id=row["parent_id"],
            budget_cap=row["budget_cap"],
            cost_accum=row["cost_accum"],
            awaiting_since=row["awaiting_since"],
            deps=self._deps(row["task_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _fetch(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row is not None else None

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
                if not all(s == TaskState.COMPLETED.value for s in dep_statuses):
                    status = TaskState.BLOCKED
            task.status = status
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"INSERT INTO tasks({_TASK_COLUMNS}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id, task.name, task.assignee, task.requester,
                    task.origin.value, task.status.value, task.description,
                    task.created_by, task.parent_id, task.budget_cap,
                    task.cost_accum, task.awaiting_since,
                    task.created_at, task.updated_at,
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
        parent_id: str | None = None,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[object] = []
        for col, val in (
            ("assignee", assignee), ("requester", requester),
            ("status", status), ("parent_id", parent_id),
        ):
            if val is not None:
                clauses.append(f"{col}=?")
                params.append(val)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks{where} ORDER BY created_at, task_id",
                params,
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    async def update_status(
        self, task_id: str, status: str, *, caller_session_id: str | None = None
    ) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            # Single-writer CAS + terminal-guard: only the assignee may write, and
            # only to a non-terminal task. rowcount == 0 → no such task, a
            # non-assignee caller, or a terminal task (incl. the abort straggler-
            # reject — Option B, #1953). Distinguish for a decision-enabling error.
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=? "
                f"WHERE task_id=? AND assignee=? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})",
                (status, _now_iso(), task_id, caller_session_id, *_TERMINAL_VALUES),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT assignee, status FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if row is None:
                    return None
                if row["status"] in _TERMINAL_VALUES:
                    raise PermissionError(
                        f"task {task_id!r} status-write rejected: task is terminal "
                        f"({row['status']}) — no further transitions (e.g. post-abort straggler)"
                    )
                raise PermissionError(
                    f"task {task_id!r} status-write rejected: caller "
                    f"{caller_session_id!r} is not the assignee (single-writer)"
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

    async def recompute_readiness(self, completed_task_id: str) -> list[Task]:
        """OS-authority readiness recompute (#1953 slice 6, OQ-3): no
        ``caller_session_id`` / no CAS (P3 scheduling, like ``abort``). Flip each
        dependent of ``completed_task_id`` from ``blocked`` → ``ready`` when ALL of
        its deps are ``completed``. The ``WHERE status='blocked'`` guard IS the
        OS-authority write (no assignee equality), distinct from ``update_status``."""
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
                    row = self._conn.execute(
                        "SELECT status FROM tasks WHERE task_id=?", (dep_task,)
                    ).fetchone()
                    if row is None or row["status"] != TaskState.BLOCKED.value:
                        continue
                    deps = self._deps(dep_task)
                    statuses = [
                        (self._conn.execute(
                            "SELECT status FROM tasks WHERE task_id=?", (d,)
                        ).fetchone() or {"status": None})["status"]
                        for d in deps
                    ]
                    if deps and all(s == TaskState.COMPLETED.value for s in statuses):
                        cur = self._conn.execute(
                            "UPDATE tasks SET status=?, updated_at=? "
                            "WHERE task_id=? AND status=?",
                            (TaskState.READY.value, _now_iso(), dep_task,
                             TaskState.BLOCKED.value),
                        )
                        if cur.rowcount:
                            self._emit(dep_task, "readiness_changed", to="ready",
                                       trigger=completed_task_id)
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
            if self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone() is None:
                return []
            # BFS the sub-tree (this task + all descendants via parent_id;
            # acyclic by construction, with a guard).
            subtree: list[str] = [task_id]
            frontier = [task_id]
            while frontier:
                pid = frontier.pop()
                for row in self._conn.execute(
                    "SELECT task_id FROM tasks WHERE parent_id=?", (pid,)
                ).fetchall():
                    child = row["task_id"]
                    if child not in subtree:
                        subtree.append(child)
                        frontier.append(child)
            self._conn.execute("BEGIN IMMEDIATE")
            for tid in subtree:
                self._conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (TaskState.ARCHIVED.value, _now_iso(), tid),
                )
                self._emit(tid, "aborted", root=task_id)
            self._conn.commit()
            return [t for t in (self._fetch(tid) for tid in subtree) if t is not None]

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
