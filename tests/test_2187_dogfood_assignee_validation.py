"""Tier 2: #2187 backend-master dogfood-fix (#45) — reject delegation to a
non-existent (agent, session).

The #45 dogfood root cause (symptom ①, gpt-oss-120b): a ``task.create`` whose
``assignee`` names no live session (e.g. the LLM used an AGENT-NAME as the bare-sid
assignee) was SILENTLY ACCEPTED — and then orphaned, because the execute-wake to a
non-resolving session is dropped (TaskWaker._wake: get_session → None → "wake
dropped"). The fix REJECTS the create up-front with a decision-enabling error, so the
orphan can never form. A self-task (assignee == caller, the live caller) is not
checked; the check is opt-in (skipped without a waker).

(Symptom ② — omitted assignee → self-default — is the correct self-task default; the
delegate-intent-omit variant is a model-usage issue tracked for the subsequent
pending-assignment feature, not this PR.)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import InMemoryTaskBackend


class _StubWaker:
    """A TaskWaker stand-in: ``resolves`` is True only for the named live sessions (or
    any ``transport:native`` routing-key); ``wake_assigned`` is a no-op (the born-
    startable delegated-task wake fires for a resolving delegation)."""

    def __init__(self, live: set[str]) -> None:
        self._live = set(live)

    def resolves(self, session_id: str) -> bool:
        return ":" in session_id or session_id in self._live

    async def wake_assigned(self, *a, **k) -> None:
        pass


def _ctx(*, waker=None, caller="s1"):
    return SimpleNamespace(
        session_id=caller, agent_id="agentA", events=None,
        task_backend=InMemoryTaskBackend(), task_waker=waker,
        task_subscription_writer=None, current_task_id=None, hook_dispatcher=None)


def _create_op(name="t", *, assignee=None):
    return SimpleNamespace(name=name, description="d", deps=[], assignee=assignee, origin="self")


@pytest.mark.asyncio
async def test_delegate_to_nonexistent_assignee_is_rejected():
    """Tier 2: the #45 repro — delegating to a bare-sid assignee that names no live
    session is REJECTED with unknown_assignee (not silently orphaned)."""
    ctx = _ctx(waker=_StubWaker(live={"s1"}))  # only s1 (the caller) is live
    res = await taskmod._create(_create_op(assignee="researcher"), ctx, "s1")
    assert res["status"] == "error" and res["error"]["kind"] == "unknown_assignee", res
    # nothing was created — no orphan
    assert await ctx.task_backend.list() == []


@pytest.mark.asyncio
async def test_delegate_to_live_assignee_ok():
    """Tier 2: delegating to a LIVE session is accepted (the legitimate cross-session
    delegation path is unaffected)."""
    ctx = _ctx(waker=_StubWaker(live={"s1", "worker-2"}))
    res = await taskmod._create(_create_op(assignee="worker-2"), ctx, "s1")
    assert res["status"] == "ok", res
    assert (await ctx.task_backend.get(res["task"]["task_id"])).assignee == "worker-2"


@pytest.mark.asyncio
async def test_self_task_skips_the_check():
    """Tier 2: a self-task (omitted assignee → the caller) is NOT checked — the caller
    is the live session making the op; self-decomposition is the common, correct path."""
    ctx = _ctx(waker=_StubWaker(live=set()))  # even with NO session registered as live
    res = await taskmod._create(_create_op(assignee=None), ctx, "s1")  # omitted → self
    assert res["status"] == "ok", res
    assert (await ctx.task_backend.get(res["task"]["task_id"])).assignee == "s1"


@pytest.mark.asyncio
async def test_no_waker_skips_the_check():
    """Tier 2: the check is opt-in — without a waker (direct construction / tests) the
    create is not gated (byte-identical to pre-fix)."""
    ctx = _ctx(waker=None)
    res = await taskmod._create(_create_op(assignee="researcher"), ctx, "s1")
    assert res["status"] == "ok", res
