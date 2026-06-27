"""Tier 2: #2187 backend-master Stage 4 — the pub/sub seam + the recovery re-read seam.

(4a) ``TaskWaker.publish_task_event`` is the single publish→deliver seam: a task
state-change event routes by ``event_type`` to the existing waker delivery (ready /
assigned → the assignee executes; terminal → the requester decides) — the local op and
an external backend publish through the SAME path.

(4b) ``AgentRegistry._reconcile_subscriptions_after_recovery`` is the recovery RE-READ
seam: after the WAL subscription replay restores the binding, it re-reads the CURRENT
backend task-state (the external master, not rewound) and returns the STALE bindings —
subscriptions whose task the backend no longer holds. (The full reconciliation —
re-publish missed events / prune — is Stage 5.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.task_wake import TaskWaker
from reyn.task import Task, TaskState


class _RoutingWaker(TaskWaker):
    """Records which delivery method publish_task_event routes to (the real dispatch)."""

    def __init__(self) -> None:  # noqa: D401 — no super (no registry needed for the dispatch test)
        self.routed: list[tuple[str, str]] = []

    async def wake_ready_dependent(self, task, **kwargs) -> None:
        self.routed.append(("ready", task.task_id))

    async def wake_assigned(self, task, **kwargs) -> None:
        self.routed.append(("assigned", task.task_id))

    async def notify_requester_decide(self, *, terminal_task, **kwargs) -> None:
        self.routed.append(("terminal", terminal_task.task_id))


def _task(tid):
    return Task(task_id=tid, name=tid, assignee="s1", requester="s1", status=TaskState.PENDING)


@pytest.mark.asyncio
async def test_publish_task_event_routes_by_type():
    """Tier 2: the single seam routes each event_type to the matching delivery (ready/
    assigned → the assignee execute; terminal → the requester decide)."""
    w = _RoutingWaker()
    await w.publish_task_event("ready", _task("t1"))
    await w.publish_task_event("assigned", _task("t2"))
    await w.publish_task_event("terminal", _task("t3"), requester_session="r", dependents=[])
    assert w.routed == [("ready", "t1"), ("assigned", "t2"), ("terminal", "t3")]


@pytest.mark.asyncio
async def test_publish_task_event_rejects_unknown_type():
    """Tier 2: an unknown event_type is a hard error (the seam's closed local vocabulary;
    external extensions add their kinds explicitly)."""
    with pytest.raises(ValueError):
        await _RoutingWaker().publish_task_event("frobnicate", _task("t1"))


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


@pytest.mark.asyncio
async def test_reconcile_after_recovery_returns_stale_bindings(tmp_path: Path):
    """Tier 2: the recovery re-read seam — a subscription whose task the backend (the
    external master) no longer holds is returned as a STALE binding; a live one is not."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory, state_log=state_log)
    # restore the binding for two tasks (the #1560 observer applies each append live).
    await state_log.append("task_subscribed", task_id="t-live", assignee="s1",
                           requester="s1", requester_kind="session")
    await state_log.append("task_subscribed", task_id="t-gone", assignee="s1",
                           requester="s1", requester_kind="session")
    # the backend (external master) holds only t-live.
    await reg.task_backend.create(_task("t-live"))

    stale = await reg._reconcile_subscriptions_after_recovery()
    assert stale == ["t-gone"]  # t-gone's binding outlived its backend state
