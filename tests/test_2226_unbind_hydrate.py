"""Tier 2: #2226 — the read-through reflects an explicit unbind (assignee → None).

The #2224 pending-queue rebinds a task's assignee via the WAL subscription
(``record_rebound``). An explicit UNBIND — ``record_rebound(assignee=None)``, the §67
re-queue primitive — must make the read-through report ``assignee is None`` (UNASSIGNED)
on BOTH backends. Before #2226 ``_hydrate_binding`` overlaid only a NON-None subscription
assignee, so an unbind fell back to the STORED placeholder (the old binding the in-memory
backend kept, or the sqlite ``""`` placeholder) → the task kept reading its OLD assignee
instead of becoming unassigned. The exists-based hydrate (a record's assignee is
authoritative, including ``None``) is the fix; this also normalizes the cross-backend read
of a #2224 UNASSIGNED task (both overlay the subscription's ``None``).
"""
from __future__ import annotations

import pytest

from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


def _backend(which: str, cp: SubscriptionRegistry, tmp_path):
    real = (
        InMemoryTaskBackend(subscription_reader=cp) if which == "inmem"
        else SqliteTaskBackend(str(tmp_path / "tasks.db"), subscription_reader=cp)
    )
    return SubscriptionBackend(real, cp)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["inmem", "sqlite"])
async def test_explicit_unbind_reads_as_unassigned(which, tmp_path):
    """Tier 2: ``record_rebound(assignee=None)`` → ``get(task).assignee is None`` on BOTH
    backends. RED before #2226: the non-None-only overlay fell back to the stored
    placeholder — the kept old binding (in-memory) or ``""`` (sqlite) — so the unbind was
    invisible and the task still read its old assignee."""
    cp = SubscriptionRegistry()
    backend = _backend(which, cp, tmp_path)
    # an ASSIGNED task — the binding (assignee=sess-A) is recorded on create.
    await backend.create(Task(task_id="t1", name="t", assignee="sess-A", requester="root",
                              status=TaskState.RUNNING))
    assert (await backend.get("t1")).assignee == "sess-A"
    # explicit unbind (the §67 re-queue primitive): record_rebound(assignee=None).
    cp.apply("task_rebound", 999, {"task_id": "t1", "assignee": None})
    got = await backend.get("t1")
    assert got.assignee is None, f"unbind not reflected — read {got.assignee!r}, expected None"


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["inmem", "sqlite"])
async def test_unassigned_task_reads_none_uniformly(which, tmp_path):
    """Tier 2: a #2224 UNASSIGNED task (created with assignee=None → the subscription
    records None) reads ``assignee is None`` on BOTH backends — the cross-backend
    normalization (sqlite previously fell back to its ``""`` placeholder)."""
    cp = SubscriptionRegistry()
    backend = _backend(which, cp, tmp_path)
    await backend.create(Task(task_id="u1", name="u", assignee=None, requester="root",
                              status=TaskState.UNASSIGNED))
    got = await backend.get("u1")
    assert got.assignee is None, f"unassigned read {got.assignee!r}, expected None"
    assert got.status is TaskState.UNASSIGNED
