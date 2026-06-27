"""Tier 2: #2187 Stage 5d — the recovery reconciliation (the backend-master recovery
model: re-subscribe, re-read the current external task-state, catch up).

PRUNE half (before session instantiation): a STALE binding (the backend no longer holds
the task — the master dropped it) is pruned live (self-healing). RE-DELIVERY half (the
§3.6 7-state predicate, after instantiation, via each live session's OWN waker): a READY
subscription re-delivers ``ready``; a RUNNING task whose awaited children settled (or a
child failed) re-delivers ``recovery_resume``; a RUNNING task still blocked on an open
awaited child re-delivers NOTHING (idle, no busy-loop). The phantom-not-cascaded backend
invariant (the 5b-deferred pin) is here too.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.task import (
    SqliteTaskBackend,
    Task,
    TaskLinkType,
    TaskRequesterKind,
    TaskState,
)
from reyn.task.subscription import SubscriptionRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


def _reg(tmp_path):
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return state_log, AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log)


async def _bind(state_log, tid, *, assignee, requester="root", kind="session"):
    await state_log.append("task_subscribed", task_id=tid, assignee=assignee,
                           requester=requester, requester_kind=kind)


def _task(tid, *, status=TaskState.READY, assignee="s1", requester="root",
          kind=TaskRequesterKind.SESSION, link=TaskLinkType.AWAITED):
    return Task(task_id=tid, name=tid, assignee=assignee, requester=requester,
                requester_kind=kind, status=status, link_type=link)


# ── PRUNE half ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_binding_is_pruned(tmp_path: Path):
    """Tier 2: a subscription whose task the backend no longer holds is PRUNED from the
    live registry (RED if prune is dropped — the stale binding would persist)."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "t-live", assignee="s1")
    await _bind(state_log, "t-gone", assignee="s1")
    await reg.task_backend.create(_task("t-live"))  # backend holds only t-live

    pruned = await reg._reconcile_subscriptions_after_recovery()

    assert pruned == ["t-gone"]
    assert not reg.task_subscriptions.exists("t-gone")  # PRUNED
    assert reg.task_subscriptions.exists("t-live")       # the live binding stays


# ── RE-DELIVERY predicate (§3.6, 7-state) ───────────────────────────────────


@pytest.mark.asyncio
async def test_ready_subscription_redelivers_ready(tmp_path: Path):
    """Tier 2: a READY task's owner is re-woken to execute (event ``ready``)."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "t-ready", assignee="s1")
    await reg.task_backend.create(_task("t-ready", status=TaskState.READY))
    work = await reg._compute_recovery_work()
    assert [(t.task_id, ev) for (t, ev) in work.get("s1", [])] == [("t-ready", "ready")]


@pytest.mark.asyncio
async def test_running_with_no_open_children_redelivers_recovery_resume(tmp_path: Path):
    """Tier 2: a RUNNING task whose awaited children settled (N_awaited==0) re-delivers
    ``recovery_resume`` (the owner resumes — continue/complete)."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "t-run", assignee="s1")
    await reg.task_backend.create(_task("t-run", status=TaskState.RUNNING))
    work = await reg._compute_recovery_work()
    assert [ev for (_t, ev) in work.get("s1", [])] == ["recovery_resume"]


@pytest.mark.asyncio
async def test_running_blocked_on_open_awaited_child_redelivers_nothing(tmp_path: Path):
    """Tier 2: a RUNNING task still blocked on an OPEN awaited child is NOT re-woken
    (idle — §3.6, no busy-loop). RED if the awaited>0 guard is dropped."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "P", assignee="s1")
    await _bind(state_log, "C", assignee="s1", requester="P", kind="task")
    await reg.task_backend.create(_task("P", status=TaskState.RUNNING))
    await reg.task_backend.create(_task("C", status=TaskState.RUNNING,
                                        kind=TaskRequesterKind.TASK, requester="P",
                                        link=TaskLinkType.AWAITED))
    work = await reg._compute_recovery_work()
    # P is NOT woken — it idles on its open awaited child C (C itself, RUNNING+childless,
    # is its own actionable resume; the assertion isolates P's idleness).
    assert [ev for (t, ev) in work.get("s1", []) if t.task_id == "P"] == []


@pytest.mark.asyncio
async def test_redelivery_uses_each_live_sessions_own_waker(tmp_path: Path):
    """Tier 2: re-delivery is SESSION-DRIVEN through each live session's OWN task_waker
    (the production waker → equivalence by construction) — only the matching session's
    work is delivered, via its publish_task_event seam."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "t-ready", assignee="s1")
    await reg.task_backend.create(_task("t-ready", status=TaskState.READY))
    work = await reg._compute_recovery_work()

    class _RecWaker:
        def __init__(self):
            self.events: list[tuple[str, str]] = []

        async def publish_task_event(self, event, task, **kw):
            self.events.append((event, task.task_id))

    waker = _RecWaker()
    reg._sessions["agentA"] = {"s1": type("S", (), {"_session_id": "s1", "_task_waker": waker})()}

    await reg._redeliver_recovery_wakes(work)
    assert waker.events == [("ready", "t-ready")]


# ── phantom-not-cascaded (the 5b-deferred pin, in-scope here) ───────────────


@pytest.mark.asyncio
async def test_phantom_binding_child_is_not_abort_cascaded(tmp_path: Path):
    """Tier 2: 5b-deferred pin — under backend-master the backend ROW is the existence
    authority — abort does NOT cascade through a PHANTOM node (a subscription binding
    whose backend row is absent), so a real grandchild under a phantom is NOT aborted.
    RED if abort walked the binding ignoring backend existence (the OLD reader-only walk)."""
    cp = SubscriptionRegistry()
    backend = SqliteTaskBackend(str(tmp_path / "t.db"), subscription_reader=cp)
    # bindings: R (root) → Ph (phantom: binding only, NO row) → G (real grandchild row).
    cp.apply("task_subscribed", 1, {"task_id": "R", "assignee": "s", "requester": "root",
                                     "requester_kind": "session"})
    cp.apply("task_subscribed", 2, {"task_id": "Ph", "assignee": "s", "requester": "R",
                                    "requester_kind": "task"})
    cp.apply("task_subscribed", 3, {"task_id": "G", "assignee": "s", "requester": "Ph",
                                    "requester_kind": "task"})
    await backend.create(_task("R"))  # R has a row
    await backend.create(_task("G", status=TaskState.READY))  # G has a row; Ph does NOT

    aborted = {t.task_id for t in await backend.abort("R")}

    assert aborted == {"R"}  # only R — the phantom Ph is not followed, so G is unreached
    assert (await backend.get("G")).status is TaskState.READY  # G untouched
    backend.close()


# ── time-travel coherence: reconcile re-adapts to CURRENT backend state ─────


@pytest.mark.asyncio
async def test_reconcile_readapts_to_current_backend_state(tmp_path: Path):
    """Tier 2: the reconcile re-reads CURRENT backend state (not a frozen view) — a parent
    that was idle (open awaited child) becomes actionable once that child reaches DONE, so
    a later reconcile picks it up. This is the backend-master recovery/time-travel
    coherence (the subscription is rewound; the backend state is re-read, not rewound)."""
    state_log, reg = _reg(tmp_path)
    await _bind(state_log, "P", assignee="s1")
    await _bind(state_log, "C", assignee="s1", requester="P", kind="task")
    await reg.task_backend.create(_task("P", status=TaskState.RUNNING))
    await reg.task_backend.create(_task("C", status=TaskState.RUNNING,
                                        kind=TaskRequesterKind.TASK, requester="P"))
    # idle: P blocked on the open awaited child C.
    before = await reg._compute_recovery_work()
    assert [ev for (t, ev) in before.get("s1", []) if t.task_id == "P"] == []

    # the child settles in the backend (the external master advances)
    await reg.task_backend.update_status("C", "done", caller_session_id="s1")

    work = await reg._compute_recovery_work()
    # P now actionable — the reconcile re-read the CURRENT (advanced) state, not a frozen one.
    assert [ev for (t, ev) in work.get("s1", []) if t.task_id == "P"] == ["recovery_resume"]
