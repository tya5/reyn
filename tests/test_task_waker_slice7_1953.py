"""Tier 2: #1953 slice 7 — the TaskWaker C3 re-invoke driver + the recovery loop.

Turns the dep-graph dispositions slice 6-ext emits into session wakes via the
canonical wake-triple ``resolve_session → _put_inbox → ensure_session_running``.
Verified with a real recording registry + recording session (no mocks):

- ``wake_ready_dependent`` / ``notify_parent_decide`` each fire the 3-step triple
  with the right OS-generic inbox kind + message;
- the op layer wakes through it: a `completed` predecessor wakes its promoted
  dependents; an `abort` / `failed` wakes the parent; a `repoint` that promotes a
  dependent (the parent's recovery move) wakes it;
- the **recovery loop** end-to-end: abort a dep → the parent is woken with
  ``task_dependency_aborted`` → it repoints the stuck dependent onto a completed
  substitute → the dependent becomes `ready` AND is woken with ``task_ready``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.runtime.services.task_wake import TaskWaker
from reyn.task import InMemoryTaskBackend, Task, TaskState


def _task(task_id, *, deps=None, status=TaskState.PENDING, assignee="sess",
          requester="req", parent_id=None):
    return Task(task_id=task_id, name=task_id, assignee=assignee, requester=requester,
                status=status, deps=list(deps or []), parent_id=parent_id)


class _RecSession:
    def __init__(self) -> None:
        self.inbox: list[tuple[str, dict]] = []

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        self.inbox.append((kind, payload))
        return "msg-1"


class _RecRegistry:
    """A real (non-mock) registry recording the wake-triple calls."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, str, str]] = []
        self.ensured: list[tuple[str, str]] = []
        self._sessions: dict[tuple[str, str, str], _RecSession] = {}

    def resolve_session(self, agent_name: str, transport: str, native_id: str):
        key = (agent_name, transport, native_id)
        self.resolved.append(key)
        return self._sessions.setdefault(key, _RecSession())

    def ensure_session_running(self, name: str, sid: str):
        self.ensured.append((name, sid))
        return None

    def inbox_of(self, agent, transport, native_id):
        return self._sessions[(agent, transport, native_id)].inbox


# ── 1. the wake-triple ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wake_ready_dependent_fires_the_triple():
    """Tier 2: wake_ready_dependent resolves the dependent's sibling session,
    delivers a `task_ready` message, and ensures the run-loop runs."""
    reg = _RecRegistry()
    waker = TaskWaker(reg, "alice")
    task = SimpleNamespace(task_id="A", name="do-A", assignee="a2a:ctx-1",
                           status=TaskState.READY)

    await waker.wake_ready_dependent(task)

    assert reg.resolved == [("alice", "a2a", "ctx-1")]      # same-agent resolve
    kind, payload = reg.inbox_of("alice", "a2a", "ctx-1")[0]
    assert kind == "task_ready" and "A" in payload["text"]
    assert reg.ensured == [("alice", "a2a:ctx-1")]          # loopless wake


@pytest.mark.asyncio
async def test_notify_parent_decide_fires_the_triple():
    """Tier 2: notify_parent_decide wakes the parent session with the disposition
    + the stuck dependents."""
    reg = _RecRegistry()
    waker = TaskWaker(reg, "alice")
    terminal = SimpleNamespace(task_id="B", name="do-B", status=TaskState.ABORTED)
    deps = [SimpleNamespace(task_id="A")]

    await waker.notify_parent_decide(parent_session="mcp:mcp", terminal_task=terminal,
                                     dependents=deps)

    assert reg.resolved == [("alice", "mcp", "mcp")]
    kind, payload = reg.inbox_of("alice", "mcp", "mcp")[0]
    assert kind == "task_dependency_aborted"
    assert "B" in payload["text"] and payload["meta"]["dependents"] == ["A"]
    assert reg.ensured == [("alice", "mcp:mcp")]


# ── 2. op-layer wakes through the waker ─────────────────────────────────────


def _ctx(backend, waker, session_id="req"):
    return SimpleNamespace(session_id=session_id, agent_id="alice", events=None,
                           task_backend=backend, task_waker=waker)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_completed_wakes_promoted_dependent():
    """Tier 2: completing a predecessor wakes the dependent the OS promoted."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    await b.create(_task("d", assignee="a2a:sd"))
    await b.create(_task("a", deps=["d"], assignee="a2a:sa"))  # born-blocked
    await taskmod._update_status(SimpleNamespace(task_id="d", status="completed"),
                                 _ctx(b, TaskWaker(reg, "alice"), session_id="a2a:sd"),
                                 "control_ir")
    assert ("alice", "a2a", "sa") in reg.resolved
    assert reg.ensured == [("alice", "a2a:sa")]


@pytest.mark.asyncio
async def test_abort_wakes_parent():
    """Tier 2: aborting a task with a stuck dependent wakes the parent."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    await b.create(_task("P", assignee="a2a:sP", status=TaskState.IN_PROGRESS))
    await b.create(_task("B", assignee="a2a:sB", parent_id="P", status=TaskState.IN_PROGRESS))
    await b.create(_task("A", deps=["B"], assignee="a2a:sA", parent_id="P"))
    await taskmod._abort(SimpleNamespace(task_id="B", reason=None),
                         _ctx(b, TaskWaker(reg, "alice")), "control_ir")
    assert ("alice", "a2a", "sP") in reg.resolved
    assert reg.inbox_of("alice", "a2a", "sP")[0][0] == "task_dependency_aborted"


# ── 3. the recovery loop (end-to-end mechanism) ─────────────────────────────


@pytest.mark.asyncio
async def test_recovery_loop_abort_then_parent_repoint_wakes_both():
    """Tier 2: the full dep-recovery loop — abort a dep → the parent is woken →
    it repoints the stuck dependent onto a completed substitute → the dependent
    becomes ready AND is woken (the close-gate at the op-mechanism level)."""
    b = InMemoryTaskBackend()
    reg = _RecRegistry()
    waker = TaskWaker(reg, "alice")
    await b.create(_task("P", assignee="a2a:sP", status=TaskState.IN_PROGRESS))
    await b.create(_task("B", assignee="a2a:sB", parent_id="P", status=TaskState.IN_PROGRESS))
    await b.create(_task("A", deps=["B"], assignee="a2a:sA", parent_id="P"))  # blocked on B
    await b.create(_task("Bsub", assignee="a2a:sBsub", parent_id="P", status=TaskState.COMPLETED))
    ctx = _ctx(b, waker)  # requester "req" owns P/B/A/Bsub

    # 1. abort B → the parent P is woken to decide.
    await taskmod._abort(SimpleNamespace(task_id="B", reason=None), ctx, "control_ir")
    assert ("alice", "a2a", "sP") in reg.resolved
    assert reg.inbox_of("alice", "a2a", "sP")[0][0] == "task_dependency_aborted"

    # 2. the parent's recovery move: repoint A from the aborted B onto the
    #    completed substitute Bsub → A becomes ready and is woken.
    await taskmod._repoint_dependency(
        SimpleNamespace(task_id="A", from_depends_on="B", to_depends_on="Bsub"),
        ctx, "control_ir")
    assert (await b.get("A")).status is TaskState.READY
    assert ("alice", "a2a", "sA") in reg.resolved
    assert reg.inbox_of("alice", "a2a", "sA")[0][0] == "task_ready"
