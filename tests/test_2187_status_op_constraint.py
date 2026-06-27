"""Tier 2: #2187 followup — the status-input constraint (dogfood-found gap).

The 5a migration renamed ``completed`` → ``done`` but left ``TaskUpdateStatusIROp.status``
an unconstrained ``str``, and the sqlite backend wrote it RAW. A weak model emitting the
stale ``"completed"`` (the dogfood gpt-oss-120b acceptance found this) was stored invalid
and corrupted the read (``TaskState(row) ValueError``), breaking the lifecycle. Two layers
now close the class: (1) the op ``Literal`` (the LLM path — OS injects the valid values,
Control-IR validation rejects a stale/invalid one); (2) the backend ``require_valid_status``
data-integrity guard (EVERY write path — A2A / direct / test — never stores a non-member).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from reyn.schemas.models import TaskUpdateStatusIROp
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState


def test_op_literal_rejects_stale_and_invalid_status():
    """Tier 2: the op constrains status to the settable subset — a stale ``"completed"``
    (or any non-member) is rejected at Control-IR validation (RED if status is a bare str)."""
    for bad in ("completed", "in_progress", "nonsense"):
        with pytest.raises(ValidationError):
            TaskUpdateStatusIROp(kind="task.update_status", task_id="t", status=bad)
    # the valid settable transitions pass.
    for ok in ("running", "done", "failed"):
        assert TaskUpdateStatusIROp(kind="task.update_status", task_id="t", status=ok).status == ok


@pytest.mark.asyncio
async def test_inmem_backend_rejects_invalid_status_never_stores():
    """Tier 2: the in-memory master rejects an invalid status BEFORE any write — the task
    is left uncorrupted (RED if the guard is stripped: the bad value would land)."""
    b = InMemoryTaskBackend()
    await b.create(Task(task_id="t", name="t", assignee="s", requester="s", status=TaskState.RUNNING))
    with pytest.raises(ValueError):
        await b.update_status("t", "completed", caller_session_id="s")
    assert (await b.get("t")).status is TaskState.RUNNING  # unchanged, not corrupted
    assert (await b.update_status("t", "done", caller_session_id="s")).status is TaskState.DONE


@pytest.mark.asyncio
async def test_sqlite_backend_rejects_invalid_status_never_stores(tmp_path):
    """Tier 2: the DURABLE (sqlite) master rejects an invalid status BEFORE the write — so
    the read stays valid (this is the exact corruption the dogfood hit: a raw invalid
    string written to the row crashed ``backend.list()``). RED if the guard is stripped."""
    b = SqliteTaskBackend(str(tmp_path / "t.db"))
    await b.create(Task(task_id="t", name="t", assignee="s", requester="s", status=TaskState.RUNNING))
    with pytest.raises(ValueError):
        await b.update_status("t", "completed", caller_session_id="s")
    # the read does NOT crash — no invalid string was stored.
    assert (await b.get("t")).status is TaskState.RUNNING
    assert [x.task_id for x in await b.list()] == ["t"]  # list() does not raise
    assert (await b.update_status("t", "done", caller_session_id="s")).status is TaskState.DONE
    b.close()
