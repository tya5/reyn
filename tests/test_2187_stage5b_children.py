"""Tier 2: #2187 Stage 5b — children_of (collision-safe), the durable link_type, and
the derived open-child counts.

``children_of(pid)`` is the shared decomposition-walk primitive (the abort DOWN-cascade
and ``open_child_counts`` both build on it): it returns pid's direct children via the
collision-safe ownership filter (``requester==pid AND requester_kind=="task"`` — finding
D; the ``task`` marker is REQUIRED because a session routing-key uuid can collide with a
task-id uuid). ``link_type`` (awaited / background) is a durable CONTENT column marked at
create. ``open_child_counts`` derives N_awaited / N_background on-demand from the
children's durable states (never separately stored).
"""
from __future__ import annotations

import pytest

from reyn.task import (
    InMemoryTaskBackend,
    SqliteTaskBackend,
    Task,
    TaskLinkType,
    TaskRequesterKind,
    TaskState,
)
from reyn.task.model import ChildCounts
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


def _child(tid, parent, link, *, status=TaskState.READY):
    return Task(task_id=tid, name=tid, assignee="s", requester=parent,
                requester_kind=TaskRequesterKind.TASK, link_type=link, status=status)


@pytest.mark.asyncio
async def test_children_of_is_collision_safe_inmem():
    """Tier 2: children_of returns only task-requester (decomposition) children — a
    SESSION-requester task whose requester key collides with the parent's id is
    EXCLUDED (the requester_kind=="task" marker, finding D). RED if the kind guard is
    stripped: X would be wrongly cascaded as a child."""
    b = InMemoryTaskBackend()
    await b.create(Task(task_id="P", name="p", assignee="s", requester="s"))
    await b.create(_child("A", "P", TaskLinkType.AWAITED))
    # a SESSION-requester task whose requester == P (uuid collision) — NOT a child.
    await b.create(Task(task_id="X", name="x", assignee="s", requester="P",
                        requester_kind=TaskRequesterKind.SESSION))
    kids = sorted(t.task_id for t in await b.children_of("P"))
    assert kids == ["A"]


@pytest.mark.asyncio
async def test_children_of_is_collision_safe_through_subscription_reader(tmp_path):
    """Tier 2: the same collision-safety through the sqlite read-through binding (the
    WAL-subscription authority — the production path), not the in-memory stored fields."""
    cp = SubscriptionRegistry()
    b = SubscriptionBackend(SqliteTaskBackend(str(tmp_path / "t.db"), subscription_reader=cp), cp)
    await b.create(Task(task_id="P", name="p", assignee="s", requester="s"))
    await b.create(_child("A", "P", TaskLinkType.AWAITED))
    await b.create(Task(task_id="X", name="x", assignee="s", requester="P",
                        requester_kind=TaskRequesterKind.SESSION))
    kids = sorted(t.task_id for t in await b.children_of("P"))
    assert kids == ["A"]
    b.close()


@pytest.mark.asyncio
async def test_link_type_durable_round_trip_sqlite(tmp_path):
    """Tier 2: link_type (a CONTENT column) survives a sqlite close + reopen — RED if
    the column is dropped from the INSERT column list or _row_to_task."""
    path = str(tmp_path / "t.db")
    cp = SubscriptionRegistry()
    b = SubscriptionBackend(SqliteTaskBackend(path, subscription_reader=cp), cp)
    await b.create(Task(task_id="P", name="p", assignee="s", requester="s"))
    await b.create(_child("bg", "P", TaskLinkType.BACKGROUND))
    await b.create(_child("aw", "P", TaskLinkType.AWAITED))
    b.close()

    reopened = SqliteTaskBackend(path, subscription_reader=cp)
    assert (await reopened.get("bg")).link_type is TaskLinkType.BACKGROUND
    assert (await reopened.get("aw")).link_type is TaskLinkType.AWAITED
    reopened.close()


@pytest.mark.asyncio
async def test_open_child_counts_splits_by_link_and_excludes_terminal():
    """Tier 2: open_child_counts derives N_awaited / N_background from the OPEN
    (non-terminal) children only — a terminal awaited child drops out of the count;
    background is counted separately; a leaf parent is (0, 0)."""
    b = InMemoryTaskBackend()
    await b.create(Task(task_id="P", name="p", assignee="s", requester="s"))
    await b.create(_child("a1", "P", TaskLinkType.AWAITED))
    await b.create(_child("a2", "P", TaskLinkType.AWAITED, status=TaskState.DONE))  # terminal → excluded
    await b.create(_child("b1", "P", TaskLinkType.BACKGROUND))
    assert await b.open_child_counts("P") == ChildCounts(awaited=1, background=1)
    assert await b.open_child_counts("a1") == ChildCounts(awaited=0, background=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["inmem", "sqlite"])
async def test_abort_full_transitive_closure(tmp_path, backend_kind):
    """Tier 2: gate-equivalence for the children_of refactor — abort(root) aborts the
    FULL transitive decomposition closure (multi-level: P → A → A1, P → B), not just the
    direct children. The closure must be identical to the pre-refactor in-line cascade."""
    cp = SubscriptionRegistry()
    real = (InMemoryTaskBackend(subscription_reader=cp) if backend_kind == "inmem"
            else SqliteTaskBackend(str(tmp_path / "t.db"), subscription_reader=cp))
    b = SubscriptionBackend(real, cp)
    await b.create(Task(task_id="P", name="p", assignee="s", requester="s"))
    await b.create(_child("A", "P", TaskLinkType.AWAITED))
    await b.create(_child("A1", "A", TaskLinkType.AWAITED))  # grandchild (transitive)
    await b.create(_child("B", "P", TaskLinkType.BACKGROUND))
    aborted = {t.task_id for t in await b.abort("P")}
    assert aborted == {"P", "A", "A1", "B"}
    if backend_kind == "sqlite":
        b.close()
