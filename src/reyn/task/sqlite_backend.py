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

Single-writer is a **compare-and-swap on ``current_run_id``** (the caller's
skill-run run_id, audit C2): ``update_status`` succeeds only when the row's
``current_run_id`` is NULL (first writer claims) or equals the writer token;
otherwise ``rowcount == 0`` → the write is rejected (a different session holds
the task). This is the durable analogue of the in-memory stub's claim.

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

from reyn.task.model import Task, TaskOrigin, TaskState, _now_iso

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
  current_run_id TEXT,
  unblock_predicate TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_links(
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY(task_id, depends_on)
);
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
    "parent_id, budget_cap, cost_accum, awaiting_since, current_run_id, version, "
    "created_at, updated_at"
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
            current_run_id=row["current_run_id"],
            version=row["version"],
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
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"INSERT INTO tasks({_TASK_COLUMNS}) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id, task.name, task.assignee, task.requester,
                    task.origin.value, task.status.value, task.description,
                    task.created_by, task.parent_id, task.budget_cap,
                    task.cost_accum, task.awaiting_since, task.current_run_id,
                    task.version, task.created_at, task.updated_at,
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
        self, task_id: str, status: str, *, writer_token: str | None = None
    ) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            # CAS (audit C2): claim if unclaimed, else require the same writer.
            # rowcount == 0 → a different session holds the task → reject.
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, current_run_id=COALESCE(current_run_id, ?), "
                "version=version+1, updated_at=? "
                "WHERE task_id=? AND (current_run_id IS NULL OR current_run_id=?)",
                (status, writer_token, _now_iso(), task_id, writer_token),
            )
            if cur.rowcount == 0:
                # Either no such task, or a CAS loss. Distinguish for the caller.
                self._conn.rollback()
                exists = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if exists is None:
                    return None
                raise PermissionError(
                    f"task {task_id!r} status-write rejected: held by another writer "
                    f"(single-writer CAS on current_run_id)"
                )
            self._emit(task_id, "status_changed", status=status, writer_token=writer_token)
            self._conn.commit()
            return self._fetch(task_id)

    async def add_dependency(self, task_id: str, depends_on: str) -> Task | None:
        async with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone() is None:
                return None
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

    async def _terminal(self, task_id: str, state: TaskState, kind: str) -> Task | None:
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (state.value, _now_iso(), task_id),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                return None
            self._emit(task_id, kind, status=state.value)
            self._conn.commit()
            return self._fetch(task_id)

    async def archive(self, task_id: str) -> Task | None:
        return await self._terminal(task_id, TaskState.ARCHIVED, "archived")

    async def abort(self, task_id: str, reason: str | None = None) -> Task | None:
        return await self._terminal(task_id, TaskState.ABORTED, "aborted")

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
