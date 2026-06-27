"""Tier 2: #2187 Stage 5a — retention is orthogonal to the lifecycle state.

The old single ARCHIVED state split into two orthogonal axes: the ABORTED lifecycle
state and the ``archived_at`` soft-delete retention marker. ``abort()`` is the
soft-delete — it sets BOTH: the task reaches ABORTED *and* gets stamped archived_at
(so the list hidden-filter, which now keys on archived_at, still hides it = the old
UX). This pins that abort sets the marker (RED if abort stops stamping archived_at —
the soft-deleted task would resurface in the list) and that the marker is durable
across a sqlite reload (RED if the column is dropped from INSERT / _row_to_task).
"""
from __future__ import annotations

import pytest

from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


@pytest.mark.asyncio
async def test_abort_sets_both_aborted_state_and_archived_at_inmem():
    """Tier 2: in-memory abort sets BOTH the ABORTED state and the archived_at
    retention marker (the two orthogonal axes), so the task is soft-deleted."""
    backend = InMemoryTaskBackend()
    await backend.create(Task(task_id="t1", name="t", assignee="s", requester="s"))
    # Before abort: a live task carries no retention marker.
    assert (await backend.get("t1")).archived_at is None

    aborted = await backend.abort("t1")

    assert aborted and aborted[0].status is TaskState.ABORTED
    # RED if abort stops stamping archived_at (→ the task would no longer be hidden).
    assert aborted[0].archived_at is not None
    got = await backend.get("t1")
    assert got.status is TaskState.ABORTED and got.archived_at is not None


@pytest.mark.asyncio
async def test_abort_archived_at_is_durable_across_sqlite_reload(tmp_path):
    """Tier 2: the archived_at retention marker survives a sqlite close + reopen — the
    soft-delete is durable, not an in-memory flag (RED if the column is dropped from
    the INSERT column list or _row_to_task)."""
    path = str(tmp_path / "tasks.db")
    cp = SubscriptionRegistry()
    backend = SubscriptionBackend(SqliteTaskBackend(path, subscription_reader=cp), cp)
    await backend.create(Task(task_id="t1", name="t", assignee="s", requester="s"))

    aborted = await backend.abort("t1")
    assert aborted and aborted[0].status is TaskState.ABORTED
    assert aborted[0].archived_at is not None
    backend.close()

    reopened = SqliteTaskBackend(path, subscription_reader=cp)
    got = await reopened.get("t1")
    assert got is not None
    assert got.status is TaskState.ABORTED
    # RED if archived_at is not persisted (the soft-delete would be lost on restart).
    assert got.archived_at is not None
    reopened.close()
