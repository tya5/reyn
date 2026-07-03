"""Tier 2: #2187 §27-31 — the pending-assignment queue (UNASSIGNED + task.assign).

(1) create-unassigned: a TOP-LEVEL create with no assignee → UNASSIGNED (it sits in the
queue, no binding, no execute-wake); an OWNED sub-task (current_task_id set) keeps the
caller default (self-decomposition is unchanged).

(2) claim-by-anyone: an UNASSIGNED task may be assigned by ANY session (queue claim) →
the binding rebinds, the status OS-derives to READY, and the new assignee is woken.

(3) reassign-CAS: an ASSIGNED task may be reassigned ONLY by its current assignee
(owner-initiated hand-off); a non-owner is denied.

(4) rebound-CAS-reads-new: after a reassign, the single-writer CAS (update_status) reads
the CURRENT (rebound) assignee — the OLD assignee is denied, the NEW one allowed. This is
the mutable-binding proof (assignee is a rebindable WAL subscription, not immutable).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import InMemoryTaskBackend, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


class _RecWaker:
    """Records publish_task_event; resolves a real (non-None) session id only."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def resolves(self, session_id) -> bool:
        return session_id is not None

    async def publish_task_event(self, event_type, task, **kwargs) -> None:
        self.events.append((event_type, task.task_id))


class _Writer:
    """Mimics the op-append SubscriptionWriter against a registry — the binding WRITES
    (record_subscribed on create / record_rebound on assign), exactly as the WAL append +
    #1560 observer do in production."""

    def __init__(self, sub: SubscriptionRegistry) -> None:
        self._sub = sub
        self._seq = 1000

    async def record_subscribed(self, task_id, *, assignee, requester, requester_kind):
        self._seq += 1
        self._sub.apply("task_subscribed", self._seq, {
            "task_id": task_id, "assignee": assignee, "requester": requester,
            "requester_kind": requester_kind})
        return self._seq

    async def record_rebound(self, task_id, *, assignee):
        self._seq += 1
        self._sub.apply("task_rebound", self._seq, {"task_id": task_id, "assignee": assignee})
        return self._seq


def _ctx(backend, waker, caller, *, writer=None, current_task_id=None):
    return SimpleNamespace(
        session_id=caller, agent_id="agentA", events=None, task_backend=backend,
        task_waker=waker, task_subscription_writer=writer,
        current_task_id=current_task_id, hook_dispatcher=None)


def _create_op(name="t", **kw):
    return SimpleNamespace(name=name, assignee=kw.get("assignee"), description=None,
                           deps=kw.get("deps", []), link_type="awaited", origin="self")


async def _fresh():
    cp = SubscriptionRegistry()
    b = SubscriptionBackend(InMemoryTaskBackend(subscription_reader=cp), cp)
    return cp, b


async def _create(b, ctx, **kw) -> str:
    res = await taskmod._create(_create_op(**kw), ctx)
    assert res.get("status") != "error", res
    return res["task"]["task_id"]


# ── (1) create-unassigned ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_level_no_assignee_is_unassigned_and_unwoken():
    """Tier 2: a TOP-LEVEL create with no assignee → UNASSIGNED, falsy assignee, NO
    execute-wake, and it shows up in the pending-assignment queue (list status=unassigned
    + subscription.unassigned()). RED if the create-unassigned path defaults to the caller
    (the old self-task default) or the #45 orphan-guard rejects the None assignee."""
    cp, b = await _fresh()
    waker = _RecWaker()
    tid = await _create(b, _ctx(b, waker, "sess-creator"))
    task = await b.get(tid)
    assert task.status is TaskState.UNASSIGNED, task.status
    assert not task.assignee, repr(task.assignee)
    assert waker.events == [], waker.events  # no execute-wake for an unassigned task
    assert tid in cp.unassigned()
    listed = await b.list(status="unassigned")
    assert [t.task_id for t in listed] == [tid]


@pytest.mark.asyncio
async def test_owned_sub_task_no_assignee_keeps_caller_default():
    """Tier 2: an OWNED sub-task (current_task_id set) with no assignee KEEPS the caller
    default (self-decomposition is unchanged — only top-level empty→UNASSIGNED). RED if the
    change blanket-unassigns every empty-assignee create."""
    cp, b = await _fresh()
    ctx = _ctx(b, _RecWaker(), "sess-worker", current_task_id="parentTask")
    tid = await _create(b, ctx)
    task = await b.get(tid)
    assert task.assignee == "sess-worker", task.assignee
    assert task.status is TaskState.READY, task.status


# ── (2) claim-by-anyone ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unassigned_task_claimed_by_any_session():
    """Tier 2: an UNASSIGNED task is claimed by a DIFFERENT session (not its creator) →
    allowed; the binding rebinds, status OS-derives READY, the claimer is woken. RED if the
    gate denies a non-owner claim, or mark_assigned does not promote UNASSIGNED→READY."""
    cp, b = await _fresh()
    waker = _RecWaker()
    tid = await _create(b, _ctx(b, waker, "sess-creator"))
    writer = _Writer(cp)
    res = await taskmod._assign(
        SimpleNamespace(task_id=tid, assignee="sess-claimer"),
        _ctx(b, waker, "sess-claimer", writer=writer))
    assert res.get("status") == "ok", res
    task = await b.get(tid)
    assert task.assignee == "sess-claimer", task.assignee
    assert task.status is TaskState.READY, task.status
    assert ("assigned", tid) in waker.events


# ── (3) reassign-CAS ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assigned_task_reassign_denied_for_non_owner_allowed_for_owner():
    """Tier 2: an ASSIGNED task — a NON-owner assign is denied (role_denied); the CURRENT
    owner may reassign (hand-off). RED if the gate is stripped (anyone could yank an
    assigned task)."""
    cp, b = await _fresh()
    waker = _RecWaker()
    writer = _Writer(cp)
    tid = await _create(b, _ctx(b, waker, "sess-A", writer=writer), assignee="sess-A")
    # non-owner (sess-B) tries to reassign → denied.
    denied = await taskmod._assign(
        SimpleNamespace(task_id=tid, assignee="sess-B"),
        _ctx(b, waker, "sess-B", writer=writer))
    assert denied.get("status") == "denied", denied
    assert (await b.get(tid)).assignee == "sess-A"
    # current owner (sess-A) hands off to sess-C → allowed.
    ok = await taskmod._assign(
        SimpleNamespace(task_id=tid, assignee="sess-C"),
        _ctx(b, waker, "sess-A", writer=writer))
    assert ok.get("status") == "ok", ok
    assert (await b.get(tid)).assignee == "sess-C"


# ── (4) rebound-CAS-reads-new (the mutable-binding proof) ─────────────────────


@pytest.mark.asyncio
async def test_status_cas_reads_rebound_assignee_not_the_old_one():
    """Tier 2: after a reassign A→C, the single-writer CAS (update_status) reads the CURRENT
    (rebound) assignee — the OLD owner (sess-A) is denied, the NEW owner (sess-C) is allowed.
    THE FALSIFICATION of "assignee immutable": this passes ONLY because the CAS reads the
    hydrated rebound binding, not a frozen birth-assignee. RED if record_rebound / the
    read-through hydrate is stripped (the CAS would still read sess-A)."""
    cp, b = await _fresh()
    waker = _RecWaker()
    writer = _Writer(cp)
    tid = await _create(b, _ctx(b, waker, "sess-A", writer=writer), assignee="sess-A")
    await taskmod._assign(
        SimpleNamespace(task_id=tid, assignee="sess-C"),
        _ctx(b, waker, "sess-A", writer=writer))
    # OLD assignee can no longer write the status.
    old = await taskmod._update_status(
        SimpleNamespace(task_id=tid, status="running"),
        _ctx(b, waker, "sess-A", writer=writer))
    assert old.get("status") == "denied", old
    # NEW assignee can.
    new = await taskmod._update_status(
        SimpleNamespace(task_id=tid, status="running"),
        _ctx(b, waker, "sess-C", writer=writer))
    assert new.get("status") == "ok", new
