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
import os
import shutil
import sqlite3
from pathlib import Path

from reyn.task.backend import find_cycle_path
from reyn.task.generation_store import SqliteTaskGenerationStore
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
  awaiting_since REAL,
  unblock_predicate TEXT,
  tools TEXT,
  result TEXT,
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
-- #2186: cross-ledger reverse-reference markers — the local index that makes a
-- cross-ledger relation discoverable from THIS ledger (the parent/target has no forward
-- list across ledgers). ``kind`` is an EXPLICIT marker (collision-safe by construction):
--   'dependent' — a cross-ledger DEPENDENT (``remote_ref``) depends on this LOCAL task
--      (``local_task_id``); on this task reaching terminal, wake the dependent (dep
--      reverse-edge propagation). The forward edge lives in the dependent's ledger.
--   'child'     — a cross-ledger OWNED sub-task (``remote_ref``) is owned by this LOCAL
--      task (``local_task_id`` = the requester/owner); on this task's abort, cascade the
--      abort to the child (§16 ownership). The ``requester`` edge lives in the child's
--      ledger.
-- ``remote_ref`` is a home-addressable task-ref → its home ledger is resolvable lookup-free
-- to route the wake/abort. Disposition/abort self-heals a stale marker (the cross-ledger
-- forward edge / requester no longer exists) by dropping it inline.
-- NOTE (#2186, do not conflate): this ``kind`` is a RELATION-TYPE discriminator on the
-- internal marker index (dependent vs child — they DISPOSE differently: dependent →
-- wake-on-completion, child → abort-on-parent-abort). It is ORTHOGONAL to the REMOVED
-- ``requester_kind`` (which was the requester's ENTITY-type session/task/external, now
-- subsumed by the self-identifying home-addressable ref). Different axis: entity-type on
-- the task = gone; relation-type on this disposition-routing index = a necessary, distinct
-- discriminator (the explicit-kind-marker discipline, applied where two relations must be
-- disambiguated rather than collision-matched).
CREATE TABLE IF NOT EXISTS task_remote_refs(
  local_task_id TEXT NOT NULL,   -- the LOCAL task the relation points at
  remote_ref TEXT NOT NULL,      -- home-addressable ref of the cross-ledger related task
  kind TEXT NOT NULL,            -- 'dependent' | 'child' (explicit collision-safe marker)
  PRIMARY KEY(local_task_id, remote_ref, kind)
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
    "task_id, name, assignee, requester, origin, status, description, "
    "created_by, awaiting_since, tools, result, created_at, updated_at"
)

# #2186: the per-session home-addressable schema generation. A db at a lower/unset
# user_version is a pre-#2186 (old-format) db → clean-break wiped on first open.
_SCHEMA_VERSION = 2


class SqliteTaskBackend:
    """Durable Task backend backed by a single sqlite database.

    Opts INTO session-rewind participation (#1953 slice R): the durable db file
    is captured per WAL boundary via ``VACUUM INTO`` and restored by replacing
    the file — symmetric with ``WorkspaceVersionStore`` (the runtime + workspace
    substrates). The rewind generations are a separate filesystem mechanism from
    the WAL ``state_log`` (P7: no new WAL kind) and from crash-recovery (the dep
    graph + decision-ops stay WAL-durable in the live db independent of them)."""

    #: #1953 slice R — this backend's durable state can be rewound (opt-in).
    supports_rewind: bool = True

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open(init_schema=True)
        self._lock = asyncio.Lock()
        # Sibling directory holding the per-generation full-DB copies.
        self._gens = SqliteTaskGenerationStore(
            Path(self._db_path).parent / "task-generations"
        )

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Apply all additive column migrations to ``tasks`` that ``CREATE TABLE IF
        NOT EXISTS`` cannot handle on its own (it does not add missing columns to an
        already-existing table).  Safe no-op on a current-schema table: the
        ``PRAGMA table_info`` guard skips any column that already exists.

        Called from ``_open(init_schema=True)`` (first open / schema init) AND from
        ``restore_to_seq`` (after the file-swap reopen), so every restored generation
        is brought to the current schema — robust to additive schema evolution by
        construction."""
        existing = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        # #1953 slice P2: additive migration for DBs created before tools / result.
        for col in ("tools", "result"):
            if col not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        conn.commit()

    def _open(self, *, init_schema: bool = False) -> sqlite3.Connection:
        """Open (or reopen, after a restore file-swap) the live connection with
        the backend's fixed pragmas.  ``init_schema=True`` on first open: creates
        tables (``CREATE TABLE IF NOT EXISTS`` is idempotent) and runs
        ``_migrate_columns`` to bring any pre-existing DB to the current schema.
        ``init_schema=False`` (restore path) skips table creation — ``restore_to_seq``
        calls ``_migrate_columns`` directly on the fresh connection after the file-swap,
        ensuring every restored generation is at the current schema."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=0")
        if init_schema:
            # #2186 clean-break (owner standing: no compat / no migration): a pre-#2186 db
            # at this path (agent-shared, bare-uuid task_ids, requester_kind column) is
            # INCOMPATIBLE with the home-addressable per-session model and is DISCARDED — no
            # old-format read, no half-old-half-new ledger. Gate on user_version so the wipe
            # is a one-time cutover (a brand-new or already-v2 db is untouched).
            self._clean_break_if_stale(conn)
            conn.executescript(_SCHEMA)
            # Single-source migration helper — also called from restore_to_seq so
            # both open and restore paths share the same column-addition logic.
            self._migrate_columns(conn)
        return conn

    @staticmethod
    def _clean_break_if_stale(conn: sqlite3.Connection) -> None:
        """#2186: one-time clean-break wipe of a pre-#2186 (old-format) Task db. Gated on
        ``PRAGMA user_version``: anything not at ``_SCHEMA_VERSION`` is wiped (DROP the task
        tables) and re-stamped, so the subsequent ``CREATE TABLE`` builds the new schema
        fresh. A brand-new db (no tables) makes the DROPs a no-op; an already-current db
        (user_version == _SCHEMA_VERSION) skips the wipe entirely. No old-format READ — only
        the structural version gate decides — honouring the owner's no-compat/no-migration
        clean-break (old in-flight tasks are abandoned on cutover)."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            return
        for tbl in ("tasks", "task_links", "task_runs", "task_events",
                    "task_comments", "task_remote_refs"):
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Rewind substrate (#1953 slice R) ─────────────────────────────────────

    async def snapshot_generation(self, seq: int) -> None:
        """Capture a full point-in-time copy of the db keyed by the WAL boundary
        ``seq``. Idempotent (a boundary already captured is left intact). The
        ``async with self._lock`` guarantees no ``BEGIN IMMEDIATE`` is open, so
        ``VACUUM INTO`` (which cannot run inside a transaction) is legal here; it
        produces a fully-checkpointed, WAL-less single-file copy that includes
        committed WAL frames. Published via tmp→rename for atomicity (a crash
        mid-copy leaves only a ``.tmp`` the next prune sweeps)."""
        dest = self._gens.gen_path(seq)
        async with self._lock:
            if dest.exists():
                return
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
            self._conn.execute("VACUUM INTO ?", (str(tmp),))
            os.replace(tmp, dest)

    async def restore_to_seq(self, seq: int) -> None:
        """Replace the live db with the generation captured at ``seq`` (the OS
        passes the nearest active seq <= the rewind target). Closes the live
        connection, copies the (WAL-less) generation file over the db path, drops
        any stale ``-wal``/``-shm`` side-files so the old WAL is not replayed over
        the restored copy, then reopens with ``_migrate_columns`` so the restored
        generation is always at the current schema — robust to additive schema
        evolution between when the generation was snapshotted and now. Re-runnable
        (idempotent under the rewind keystone). Defensive no-op if the generation
        is missing."""
        src = self._gens.gen_path(seq)
        async with self._lock:
            if not src.exists():
                return
            self._conn.close()
            shutil.copy2(src, self._db_path)
            for side in ("-wal", "-shm"):
                stale = Path(self._db_path + side)
                if stale.exists():
                    stale.unlink()
            self._conn = self._open()
            self._migrate_columns(self._conn)

    async def generation_seqs(self) -> list[int]:
        async with self._lock:
            return self._gens.seqs()

    async def prune_generations_below(self, min_keep_seq: int) -> int:
        async with self._lock:
            return self._gens.prune_below(min_keep_seq)

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
            if row is None or row["status"] != TaskState.COMPLETED.value:
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
        if status not in (
            TaskState.PENDING.value, TaskState.READY.value, TaskState.BLOCKED.value
        ):
            return None
        satisfied = self._all_deps_completed(task_id)
        if satisfied and status == TaskState.BLOCKED.value:
            new = TaskState.READY.value
        elif allow_demote and not satisfied and status in (
            TaskState.PENDING.value, TaskState.READY.value
        ):
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
        return Task(
            task_id=row["task_id"],
            name=row["name"],
            assignee=row["assignee"],
            requester=row["requester"],
            origin=TaskOrigin(row["origin"]),
            status=TaskState(row["status"]),
            description=row["description"],
            created_by=row["created_by"],
            awaiting_since=row["awaiting_since"],
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
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.task_id, task.name, task.assignee, task.requester,
                    task.origin.value, task.status.value,
                    task.description, task.created_by, task.awaiting_since,
                    json.dumps(task.tools) if task.tools else None, task.result,
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
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[object] = []
        for col, val in (
            ("assignee", assignee), ("requester", requester),
            ("status", status),
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

    # ── #2186 cross-ledger reverse-reference markers ─────────────────────────

    async def add_remote_ref(
        self, local_task_id: str, remote_ref: str, kind: str, *, durable: bool = True,
    ) -> None:
        """#2186: record a cross-ledger reverse-reference marker in THIS ledger — the
        local index that makes a cross-ledger relation discoverable from here (the
        parent/target has no forward list across ledgers). ``kind`` is 'dependent'
        (``remote_ref`` depends on ``local_task_id`` → wake it on completion) or 'child'
        (``remote_ref`` is owned by ``local_task_id`` → abort it on parent abort).

        ``durable=True`` runs a ``wal_checkpoint(FULL)`` after commit so the marker AND
        every prior committed write in this ledger are power-loss-durable BEFORE the
        caller proceeds to the cross-ledger forward write — the R1 ordering barrier
        (cross-ledger edges only): dep → the TARGET (+ its dependent-marker) durable before
        the forward edge in the dependent's ledger; ownership → the owner-MARKER durable
        before the child is created in the assignee's ledger. The dangling that can survive
        a crash is thus always the BENIGN direction (a marker without its forward edge /
        child → a no-op wake / abort, self-healed inline)."""
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR IGNORE INTO task_remote_refs(local_task_id, remote_ref, kind) "
                "VALUES (?,?,?)",
                (local_task_id, remote_ref, kind),
            )
            self._conn.commit()
            if durable:
                # Flush the WAL to the main db → power-loss-durable (the task db runs
                # synchronous=NORMAL, so a bare commit is app-crash- but not
                # power-loss-durable until a checkpoint). Sole connection per session →
                # no checkpoint contention.
                self._conn.execute("PRAGMA wal_checkpoint(FULL)")

    async def remote_refs(self, local_task_id: str, kind: str) -> list[str]:
        """#2186: the home-addressable refs of this LOCAL task's cross-ledger related
        tasks of ``kind`` — the discovery set for a cross-ledger wake ('dependent') or
        abort-cascade ('child')."""
        async with self._lock:
            return [
                r["remote_ref"] for r in self._conn.execute(
                    "SELECT remote_ref FROM task_remote_refs WHERE local_task_id=? AND kind=?",
                    (local_task_id, kind),
                ).fetchall()
            ]

    async def drop_remote_ref(self, local_task_id: str, remote_ref: str, kind: str) -> None:
        """#2186: drop a stale cross-ledger reverse-marker (the wake/abort-time self-heal:
        the cross-ledger forward edge / requester no longer exists → the marker is an
        orphan → drop it so it can't fire a second benign no-op). Strict zero-orphan."""
        async with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "DELETE FROM task_remote_refs WHERE local_task_id=? AND remote_ref=? AND kind=?",
                (local_task_id, remote_ref, kind),
            )
            self._conn.commit()

    async def add_remote_dependency(self, task_id: str, depends_on: str) -> "Task | None":
        """#2186: record a CROSS-LEDGER forward dep edge (``task_id`` depends on
        ``depends_on``, which lives in ANOTHER ledger) in THIS (the dependent's) ledger,
        WITHOUT the local existence / cycle check (the target is remote — the op layer
        validated its existence cross-ledger + wrote the R1-durable dependent-marker in the
        target's ledger first). A fresh cross-ledger dep is not-yet-known-completed, so the
        dependent is BLOCKED here; it is re-derived (and promoted) when the cross-ledger
        completion wakes it (the op layer resolves the remote dep's status via the
        resolver at that point). ``None`` if ``task_id`` is absent."""
        async with self._lock:
            if not self._exists(task_id):
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR IGNORE INTO task_links(task_id, depends_on) VALUES (?,?)",
                (task_id, depends_on),
            )
            # A new cross-ledger dep can only block (a completion would arrive via the
            # cross-ledger wake, never silently) — re-block pre-run states.
            self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=? "
                "WHERE task_id=? AND status IN (?,?,?)",
                (TaskState.BLOCKED.value, _now_iso(), task_id,
                 TaskState.PENDING.value, TaskState.READY.value, TaskState.BLOCKED.value),
            )
            self._emit(task_id, "dependency_added", depends_on=depends_on, cross_ledger=True)
            self._conn.commit()
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
            # edge child→pid is followed when requester=pid. #2186: ``pid`` is a
            # home-addressable task reference (``task:...``), which CANNOT collide with a
            # session routing-key (a session/external requester is never a task-ref form),
            # so the bare ``requester=pid`` match is collision-safe BY CONSTRUCTION — the
            # old ``requester_kind='task'`` guard is subsumed (the kind moved INTO the ref
            # form). NOTE (#2186 cross-ledger): this BFS is LOCAL to this session's ledger;
            # a sub-task DELEGATED to another session lives in that session's ledger and is
            # reached via the cross-ledger ownership cascade (handled at the op layer).
            # Acyclic (one requester/task → earlier task) + the in-set guard → bounded.
            # In-lock BFS (NOT recursive — the lock is not re-entrant).
            to_abort: list[str] = [task_id]
            frontier = [task_id]
            while frontier:
                pid = frontier.pop()
                for row in self._conn.execute(
                    "SELECT task_id FROM tasks WHERE requester=?",
                    (pid,),
                ).fetchall():
                    child = row["task_id"]
                    if child not in to_abort:
                        to_abort.append(child)
                        frontier.append(child)
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
            for tid in to_abort:
                self._conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (TaskState.ARCHIVED.value, _now_iso(), tid),
                )
                self._emit(tid, "aborted", root=task_id)
            self._conn.commit()
            return [t for t in (self._fetch(tid) for tid in to_abort) if t is not None]


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
