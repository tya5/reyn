"""Tier 2: #1953 dynamic-wire item-1 — the gate-equivalence / no-bypass proof.

The 11 task.* ops are exposed via ``invoke_action`` (task__update_status, …).
This proves the NEW router-dispatch path (tools/task_ops.py factory →
execute_op) enforces the SAME assignee single-writer CAS as the phase path —
keyed on the REAL ``OpContext.session_id`` threaded through the router host
factory — with NO None-placeholder mask-pass.

Falsification design (lead's flag): a None/placeholder session_id would
``!= assignee`` and so reject EVERY write, including the rightful assignee's.
The ``assignee → allowed`` case is therefore the catch: it passes ONLY when the
real caller session id reaches the CAS. Paired with ``non-assignee → denied``,
the two prove byte-equal-to-phase enforcement (the phase path's
``test_cas_reject_end_to_end_through_op_layer`` is the same assertion via the
op layer directly).
"""
from __future__ import annotations

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.core.op_runtime.context import OpContext
from reyn.security.permissions.permissions import PermissionDecl
from reyn.task import InMemoryTaskBackend
from reyn.tools.task_ops import TASK_TOOL_DEFINITIONS
from reyn.tools.types import RouterCallerState, ToolContext

_TASK_DEFS = {d.name: d for d in TASK_TOOL_DEFINITIONS}
_UPDATE = _TASK_DEFS["task.update_status"].handler


class _Events:
    """Minimal event sink (execute_op emits permission_denied / ok events)."""

    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    def emit(self, type: str, **data) -> None:  # mirror EventLog.emit signature
        self.emitted.append((type, data))


def _op_ctx(session_id: str | None, backend) -> OpContext:
    """A real router-style OpContext carrying the caller session + Task backend
    (what RouterHostAdapter.make_router_op_context threads in production)."""
    return OpContext(
        workspace=None,
        events=_Events(),
        permission_decl=PermissionDecl(),
        session_id=session_id,
        task_backend=backend,
    )


def _tool_ctx(router_state: RouterCallerState | None) -> ToolContext:
    """A router-kind ToolContext (the non-task fields are unused by the task
    bridge, which reads only router_state.op_context_factory / phase op_context)."""
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=router_state,
    )


def _router_ctx(op_ctx: OpContext) -> ToolContext:
    """A ToolContext whose router factory yields the given OpContext (mirrors
    ctx.router_state.op_context_factory = host.make_router_op_context)."""
    return _tool_ctx(RouterCallerState(op_context_factory=lambda: op_ctx))


async def _task_assigned_to(backend, assignee: str) -> str:
    """Create a task whose assignee (single writer) is ``assignee``."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(task_backend=backend, session_id=assignee,
                          agent_id="a", events=None)
    created = await taskmod._create(
        SimpleNamespace(name="t", assignee=assignee, requester=assignee,
                        origin="self", description=None, deps=[]),
        ctx
    )
    return created["task"]["task_id"]


@pytest.mark.asyncio
async def test_non_assignee_update_status_via_invoke_action_denied():
    """Tier 2: a NON-assignee task__update_status through the invoke_action
    handler hits the CAS and is rejected — byte-equal to the phase path (both go
    through execute_op, which surfaces the CAS PermissionError as a structured
    ``status="denied"`` result). Proves the router path does NOT bypass the
    single-writer gate."""
    backend = InMemoryTaskBackend()
    task_id = await _task_assigned_to(backend, "sess-B")
    # caller session = sess-A, NOT the assignee sess-B.
    ctx = _router_ctx(_op_ctx("sess-A", backend))
    result = await _UPDATE({"task_id": task_id, "status": "failed"}, ctx)
    assert result.get("status") == "denied", result


@pytest.mark.asyncio
async def test_assignee_update_status_via_invoke_action_allowed():
    """Tier 2: the ASSIGNEE's task__update_status through the invoke_action
    handler is allowed. THE FALSIFICATION: a None/placeholder session_id would
    reject even the rightful assignee — this passes ONLY because the REAL caller
    session id (sess-B) threads through the router OpContext to the CAS."""
    backend = InMemoryTaskBackend()
    task_id = await _task_assigned_to(backend, "sess-B")
    ctx = _router_ctx(_op_ctx("sess-B", backend))  # caller == assignee
    result = await _UPDATE({"task_id": task_id, "status": "running"}, ctx)
    assert result.get("status") == "ok", result


@pytest.mark.asyncio
async def test_none_session_id_masks_and_rejects_even_the_assignee():
    """Tier 2: DIRECT falsification of the mask. If the router OpContext carried
    ``session_id=None`` (the placeholder mask the gate-threading prevents), even
    the rightful assignee (sess-B) is rejected — ``None != "sess-B"``. This
    demonstrates WHY the ``assignee → allowed`` test above is the real proof: it
    passes ONLY because the REAL caller session id reaches the CAS, not a mask."""
    backend = InMemoryTaskBackend()
    task_id = await _task_assigned_to(backend, "sess-B")
    ctx = _router_ctx(_op_ctx(None, backend))  # the mask: session_id is None
    result = await _UPDATE({"task_id": task_id, "status": "running"}, ctx)
    assert result.get("status") == "denied", result


@pytest.mark.asyncio
async def test_no_session_context_refuses_rather_than_mask():
    """Tier 2: when NO real-session OpContext is available (no router factory,
    no phase op_context), the bridge REFUSES (no_session_context) rather than
    fall back to a session-less context that would mask-pass the CAS gate
    (no-bypass-by-construction)."""
    backend = InMemoryTaskBackend()
    task_id = await _task_assigned_to(backend, "sess-B")
    ctx = _tool_ctx(router_state=None)  # no router factory + no phase op_context
    # a VALID status (the op Literal now constrains it, #2187 followup) so the op passes
    # arg-validation and the test exercises the no-session-context refusal.
    result = await _UPDATE({"task_id": task_id, "status": "running"}, ctx)
    assert result.get("error_kind") == "no_session_context", result


@pytest.mark.asyncio
async def test_router_opctx_threads_task_waker_so_chat_abort_wakes_requester():
    """Tier 2: #2107 LIVE chat path — make_router_op_context MUST thread the TaskWaker so a
    chat task__abort actually WAKES the requester. task_waker was the ONE op-ctx field the
    chat-router builder did not thread (the #1953 wire threaded task_backend but not
    task_waker), so a live chat recovery wake was silently skipped (ctx.task_waker=None)
    despite the routing + bare-sid fix being correct. This goes through the REAL
    make_router_op_context (not a hand-built OpContext). Strip the wire → ctx.task_waker is
    None → the requester is never woken → RED."""
    from types import SimpleNamespace

    from reyn.task import Task, TaskState
    from tests._support.router_host_adapter import make_adapter

    class _RecWaker:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def notify_requester_decide(self, **kw) -> None:
            self.calls.append(kw)

        async def publish_task_event(self, event_type, task, **kwargs) -> None:
            # #2187 Stage 4: the op publishes through the single seam; route the
            # terminal event to the recorded requester-notify.
            if event_type == "terminal":
                await self.notify_requester_decide(terminal_task=task, **kwargs)

    backend = InMemoryTaskBackend()
    # a self-task plan whose requester is the chat session ("main", _DEFAULT_SID).
    await backend.create(Task(task_id="t2", name="t2", assignee="main", requester="main",
                              status=TaskState.RUNNING, deps=[]))
    await backend.create(Task(task_id="t3", name="t3", assignee="main", requester="main",
                              deps=["t2"]))

    waker = _RecWaker()
    adapter = make_adapter(agent_name="alice", task_backend=backend,
                           task_waker=waker, session_id="main")
    ctx = adapter.make_router_op_context()

    assert ctx.task_waker is waker  # the wire — the exact gap make_router_op_context had

    # abort t2 through the REAL router op-ctx → the requester ("main") must be woken.
    await taskmod._abort(SimpleNamespace(task_id="t2", reason=None), ctx)
    assert waker.calls and waker.calls[0]["requester_session"] == "main"
