"""Tier 2: #2137 — restore_to_seq re-runs schema migration via _migrate_columns.

Invariant: a generation snapshotted before an additive column existed is
automatically brought to the current schema on restore, so _row_to_task (and any
reader that accesses the column) never hits an OperationalError or KeyError on the
restored db.

Mechanism under test: SqliteTaskBackend.restore_to_seq calls _migrate_columns on the
freshly reopened connection after the file-swap — the same helper _open uses on first
open.  The test constructs a cross-version restore scenario directly: create a backend,
snapshot a generation at seq 1, then open the generation file with a raw sqlite3
connection and DROP the requester_kind column to simulate a pre-column snapshot.
Restore to that generation and assert the column is re-added and a task with a
NON-default requester_kind (TaskRequesterKind.TASK) is readable.

Falsification (documented below): removing the _migrate_columns call from
restore_to_seq causes the test to go RED with an OperationalError on the INSERT
(missing column) or on the SELECT (_row_to_task accesses requester_kind by name).
"""
from __future__ import annotations

import sqlite3

import pytest

from reyn.task import Task, TaskState
from reyn.task.model import TaskRequesterKind
from reyn.task.sqlite_backend import SqliteTaskBackend


@pytest.mark.asyncio
async def test_restore_re_runs_schema_migration_cross_version(tmp_path):
    """Tier 2: restore_to_seq re-migrates a pre-column generation — non-default
    requester_kind survives round-trip through a cross-version restore.

    Setup:
      1. Create a backend and snapshot a generation at seq 1 (current schema,
         with requester_kind).
      2. Surgically remove the requester_kind column from the generation FILE via a
         raw sqlite3 connection, simulating a snapshot taken before that column was
         added (the generation file is an independent copy — editing it does not
         touch the live db).
      3. restore_to_seq(1) — should replace the live db with the old-schema file and
         then call _migrate_columns, re-adding the column.
      4. Insert a task with requester_kind=TaskRequesterKind.TASK (non-default;
         the default is 'session') and read it back via get() / _row_to_task.
      5. Assert the returned task carries the non-default value — proves the column
         exists and is wired end-to-end.

    Falsification: with the _migrate_columns call removed from restore_to_seq, step 4
    raises sqlite3.OperationalError ("table tasks has no column named requester_kind")
    because the restored db still lacks the column — test goes RED, confirming the
    call is load-bearing.
    """
    db_path = tmp_path / "tasks.db"
    b = SqliteTaskBackend(db_path)

    # Populate a task at seq 1 and snapshot.
    await b.create(Task(
        task_id="pre-col-task",
        name="pre",
        assignee="s",
        requester="r",
        status=TaskState.PENDING,
    ))
    await b.snapshot_generation(1)

    # Locate the generation file and surgically remove requester_kind to simulate a
    # pre-column snapshot.  We use a raw sqlite3 connection on the GENERATION FILE
    # (not the live db) so the live connection is unaffected.
    gen_file = b._gens.gen_path(1)
    assert gen_file.exists(), "generation file must exist after snapshot_generation"
    with sqlite3.connect(str(gen_file)) as raw:
        # Verify the column exists before we remove it (guards the test setup).
        cols_before = {r[1] for r in raw.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "requester_kind" in cols_before, (
            "generation file should have requester_kind before we strip it"
        )
        raw.execute("ALTER TABLE tasks DROP COLUMN requester_kind")
        raw.commit()
        cols_after = {r[1] for r in raw.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "requester_kind" not in cols_after, (
            "requester_kind must be gone after DROP COLUMN (simulates pre-column snapshot)"
        )

    # Restore to the stripped generation — _migrate_columns must re-add the column.
    await b.restore_to_seq(1)

    # After restore the live db should be at the current schema.  Insert a task with
    # a NON-default requester_kind to prove the column is present AND correctly wired
    # through _row_to_task.
    await b.create(Task(
        task_id="post-restore-task",
        name="post",
        assignee="s",
        requester="r",
        requester_kind=TaskRequesterKind.TASK,   # non-default (default='session')
        status=TaskState.PENDING,
    ))
    fetched = await b.get("post-restore-task")
    assert fetched is not None, "task must be readable after restore"
    assert fetched.requester_kind == TaskRequesterKind.TASK, (
        f"non-default requester_kind must round-trip; got {fetched.requester_kind!r}"
    )

    b.close()
