"""Tier 2: #1953 slice 2 — durable sqlite Task backend.

Real sqlite (no fake/mock backend — a fake misses construction bugs, the test
mandate for this slice). Covers: non-default round-trip across a reload from
disk, a blocked task with ``awaiting_since`` persisting, the single-writer CAS
on ``current_run_id`` (audit C2), and the own ``task_events`` projection.

Falsification:
- the reload test reds if any non-default field is dropped on write or read
  (a real construction bug a fake backend would hide).
- the CAS test reds if a second writer's ``update_status`` is allowed through
  (single-writer broken).
"""
from __future__ import annotations

import pytest

from reyn.task import SqliteTaskBackend, Task, TaskOrigin, TaskState


def _db(tmp_path) -> str:
    return str(tmp_path / "nested" / "tasks.db")  # nested → exercises mkdir


@pytest.mark.asyncio
async def test_nondefault_task_round_trips_across_reload_from_disk(tmp_path):
    """Tier 2: a fully non-default task survives a close + reopen from disk."""
    path = _db(tmp_path)
    backend = SqliteTaskBackend(path)
    task = Task(
        task_id="t-1", name="ship", assignee="bob", requester="alice",
        origin=TaskOrigin.EXTERNAL, status=TaskState.BLOCKED,
        description="do the thing", created_by="alice", parent_id="p-0",
        budget_cap=42.5, cost_accum=3.5, awaiting_since=1234.5,
        deps=["d-1", "d-2"],
    )
    await backend.create(task)
    backend.close()

    # Reopen a fresh backend on the same file — durability, not in-memory cache.
    reopened = SqliteTaskBackend(path)
    got = await reopened.get("t-1")
    assert got is not None
    # RED if any non-default field is dropped on write or read.
    assert got.name == "ship"
    assert got.assignee == "bob"
    assert got.requester == "alice"
    assert got.origin is TaskOrigin.EXTERNAL
    assert got.status is TaskState.BLOCKED
    assert got.description == "do the thing"
    assert got.created_by == "alice"
    assert got.parent_id == "p-0"
    assert got.budget_cap == 42.5
    assert got.cost_accum == 3.5
    assert got.awaiting_since == 1234.5
    assert got.deps == ["d-1", "d-2"]
    reopened.close()


@pytest.mark.asyncio
async def test_list_filters_persist(tmp_path):
    """Tier 2: list filters work against the persisted rows."""
    backend = SqliteTaskBackend(_db(tmp_path))
    await backend.create(Task(task_id="a", name="a", assignee="bob", requester="r"))
    await backend.create(Task(task_id="b", name="b", assignee="carol", requester="r"))

    bob = await backend.list(assignee="bob")
    assert [t.task_id for t in bob] == ["a"]
    assert await backend.list(assignee="nobody") == []
    backend.close()


@pytest.mark.asyncio
async def test_update_status_cas_claims_then_rejects_other_writer(tmp_path):
    """Tier 2: the first writer claims via current_run_id; a second writer with
    a different token is rejected (single-writer CAS, audit C2)."""
    backend = SqliteTaskBackend(_db(tmp_path))
    await backend.create(Task(task_id="t", name="n", assignee="bob", requester="r"))

    # First writer claims the task.
    claimed = await backend.update_status("t", "in_progress", writer_token="run-A")
    assert claimed is not None
    assert claimed.current_run_id == "run-A"
    assert claimed.status is TaskState.IN_PROGRESS

    # Same writer may continue.
    again = await backend.update_status("t", "completed", writer_token="run-A")
    assert again is not None and again.status is TaskState.COMPLETED

    # A different writer is rejected — single-writer CAS holds.
    with pytest.raises(PermissionError):
        await backend.update_status("t", "failed", writer_token="run-B")

    # State is unchanged by the rejected write.
    after = await backend.get("t")
    assert after is not None and after.status is TaskState.COMPLETED
    backend.close()


@pytest.mark.asyncio
async def test_update_status_unknown_task_returns_none(tmp_path):
    """Tier 2: update on a missing task returns None (not a CAS reject)."""
    backend = SqliteTaskBackend(_db(tmp_path))
    assert await backend.update_status("nope", "in_progress", writer_token="x") is None
    backend.close()


@pytest.mark.asyncio
async def test_awaiting_and_archive_persist_and_emit_events(tmp_path):
    """Tier 2: set_awaiting + archive persist, and the own task_events projection
    records each state change (the backend is the source of truth)."""
    path = _db(tmp_path)
    backend = SqliteTaskBackend(path)
    await backend.create(Task(task_id="t", name="n", assignee="b", requester="r",
                              status=TaskState.BLOCKED))
    await backend.set_awaiting("t", 999.0)
    await backend.archive("t")
    backend.close()

    reopened = SqliteTaskBackend(path)
    got = await reopened.get("t")
    assert got is not None
    assert got.awaiting_since == 999.0
    assert got.status is TaskState.ARCHIVED

    kinds = [e["kind"] for e in await reopened.events("t")]
    # created + awaiting_set + archived recorded in the backend's own projection.
    assert kinds == ["created", "awaiting_set", "archived"]
    reopened.close()


@pytest.mark.asyncio
async def test_add_dependency_and_comment(tmp_path):
    """Tier 2: dependency edges + comments persist."""
    backend = SqliteTaskBackend(_db(tmp_path))
    await backend.create(Task(task_id="t", name="n", assignee="b", requester="r"))
    updated = await backend.add_dependency("t", "u")
    assert updated is not None and updated.deps == ["u"]

    cid = await backend.add_comment("t", "bob", "looks good")
    assert cid is not None
    assert await backend.add_comment("missing", "bob", "x") is None
    backend.close()
