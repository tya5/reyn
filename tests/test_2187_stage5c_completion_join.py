"""Tier 2: #2187 Stage 5c — the completion-join gate + the child_settled reconciler.

(1) completion-join: a task may reach DONE only when its decomposition tree is complete
(no open child of EITHER link type — awaited gates execution, but the final DONE requires
the whole tree terminal). Attempting DONE with open children returns a DECISION-ENABLING
error (the LLM is not stopped — it waits or aborts the children).

(2) child_settled reconciler: when a decomposition child settles, its PARENT is woken via
the requester_kind-EXCLUSIVE routing (TASK → child_settled alone, subsuming recovery; no
double-wake by construction). Mutually-exclusive firing: total==0 → final completion;
elif awaited==0 → continue; elif the child failed → recovery; else the parent idles.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import (
    InMemoryTaskBackend,
    Task,
    TaskLinkType,
    TaskRequesterKind,
    TaskState,
)
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


class _RecWaker:
    """Records publish_task_event(event_type, task, **kwargs) — the real dispatch seam."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def resolves(self, session_id: str) -> bool:
        return True

    async def publish_task_event(self, event_type, task, **kwargs) -> None:
        self.events.append((event_type, task.task_id, kwargs))


def _ctx(backend, waker, caller):
    return SimpleNamespace(
        session_id=caller, agent_id="agentA", events=None, task_backend=backend,
        task_waker=waker, task_subscription_writer=None, current_task_id=None,
        hook_dispatcher=None)


def _child(tid, parent, link, *, assignee, status=TaskState.READY):
    return Task(task_id=tid, name=tid, assignee=assignee, requester=parent,
                requester_kind=TaskRequesterKind.TASK, link_type=link, status=status)


async def _backend_with(*tasks):
    cp = SubscriptionRegistry()
    b = SubscriptionBackend(InMemoryTaskBackend(subscription_reader=cp), cp)
    for t in tasks:
        await b.create(t)
    return b


async def _complete(backend, waker, task_id, assignee, *, status="done"):
    return await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status=status), _ctx(backend, waker, assignee))


# ── (1) completion-join gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_join_blocks_done_with_open_awaited_child():
    """Tier 2: a parent with an OPEN AWAITED child cannot reach DONE — a decision-
    enabling open_children error (RED if the completion-join gate is stripped)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA"))
    res = await _complete(b, _RecWaker(), "P", "sP")
    assert res["status"] == "error" and res["error"]["kind"] == "open_children", res
    assert res["error"]["awaited"] == 1 and res["error"]["background"] == 0
    # the parent did NOT transition — still completable later.
    assert (await b.get("P")).status is not TaskState.DONE


@pytest.mark.asyncio
async def test_completion_join_blocks_done_with_open_background_child():
    """Tier 2: a BACKGROUND child also blocks the final DONE (the whole tree must be
    terminal — background never blocks execution, but it gates completion)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("B", "P", TaskLinkType.BACKGROUND, assignee="sB"))
    res = await _complete(b, _RecWaker(), "P", "sP")
    assert res["status"] == "error" and res["error"]["kind"] == "open_children", res
    assert res["error"]["background"] == 1


@pytest.mark.asyncio
async def test_completion_join_allows_done_when_no_open_children():
    """Tier 2: with the tree complete (children terminal / none), DONE succeeds."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA", status=TaskState.DONE))
    res = await _complete(b, _RecWaker(), "P", "sP")
    assert res["status"] == "ok", res
    assert (await b.get("P")).status is TaskState.DONE


# ── (2) child_settled reconciler ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_awaited_child_done_wakes_parent_final_when_last():
    """Tier 2: completing the last awaited child reconciles the parent with
    reason=final_completion (total==0 → the parent may complete)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA"))
    w = _RecWaker()
    await _complete(b, w, "A", "sA")
    assert w.events == [("child_settled", "P", w.events[0][2])]
    assert w.events[0][2]["reason"] == "final_completion"


@pytest.mark.asyncio
async def test_awaited_cleared_but_background_open_wakes_parent_continue():
    """Tier 2: clearing the awaited child while a background child is still open wakes
    the parent with reason=continue (awaited==0 but total>0 — unblocked, not complete)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA"),
        _child("B", "P", TaskLinkType.BACKGROUND, assignee="sB"))
    w = _RecWaker()
    await _complete(b, w, "A", "sA")
    assert w.events[-1][0] == "child_settled" and w.events[-1][2]["reason"] == "continue"


@pytest.mark.asyncio
async def test_open_awaited_sibling_means_parent_not_woken():
    """Tier 2: completing one awaited child while another awaited child is still open
    does NOT wake the parent (awaited>0, no failure → the parent idles, §3.5)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA"),
        _child("A2", "P", TaskLinkType.AWAITED, assignee="sA2"))
    w = _RecWaker()
    await _complete(b, w, "A", "sA")
    assert w.events == []


@pytest.mark.asyncio
async def test_failed_task_child_routes_only_to_child_settled_no_double_wake():
    """Tier 2: a FAILED decomposition child routes EXCLUSIVELY to child_settled — NOT
    also the legacy terminal/notify_requester_decide path (the requester_kind-exclusive
    routing eliminates the double-wake by construction)."""
    b = await _backend_with(
        Task(task_id="P", name="p", assignee="sP", requester="root"),
        _child("A", "P", TaskLinkType.AWAITED, assignee="sA"))
    w = _RecWaker()
    await _complete(b, w, "A", "sA", status="failed")
    kinds = [e[0] for e in w.events]
    assert kinds == ["child_settled"]  # exactly one wake, not ["terminal", "child_settled"]
    assert "terminal" not in kinds
