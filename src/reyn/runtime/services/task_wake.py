"""TaskWaker — the OS C3 re-invoke driver for the Task dep-graph (#1953 slice 7).

Turns the dep-graph dispositions that slice 6-ext emits into actual **session
wakes**, via the canonical wake-triple ``resolve_session → _put_inbox →
ensure_session_running`` (the same pattern ``webhook_routing`` /
``gateway.api.push_to_agent`` use). Threaded onto ``OpContext.task_waker`` (the
slice 6-ext stub) like ``task_backend``.

Two drivers:
  - ``wake_ready_dependent(task)`` — complete-side: a dependent the OS promoted to
    ``ready`` is woken to continue its work.
  - ``notify_parent_decide(...)`` — abort/failed-side: the parent of a terminal
    task with stuck dependents is woken to decide recovery (it re-wires via
    ordinary task ops — NOT a ``decision=`` vocabulary, P7).

Single-agent scope: a dependency lives WITHIN one decomposition = within one
agent (the owner model: a cross-agent hand-off is a *request*, not a dep-edge), so
the target session is a sibling session of THIS agent — the waker closes over its
own ``agent_name`` and resolves the assignee/parent session-id (the #1814
``<transport>:<native_id>`` routing-key) within it.

Loopless wake: an A2A (``a2a:<contextId>``) / MCP (``mcp:mcp``) session has no
run-loop (its turns are driven inline by ``MessageBus.request``), so
``ensure_session_running`` is **mandatory** to boot ``session.run()`` and drain
the wake message. Idempotent for an already-looping session (the ``.done()``
guard). This in-process driver is distinct from the external A2A webhook *sweep*
(cross-process, backend-list-derived) — complementary layers.
"""
from __future__ import annotations

from typing import Any

# The OS-generic inbox kinds (P7 — no skill/A2A vocabulary; the run-loop routes
# them to a router turn). NOT added to the WAL closed vocab (WAL-vs-P6 separation).
WAKE_READY_KIND = "task_ready"
WAKE_PARENT_KIND = "task_dependency_aborted"


class TaskWaker:
    """Wakes a sibling session (same agent) on a dep-graph disposition (#1953 sl.7)."""

    def __init__(self, registry: Any, agent_name: str) -> None:
        self._registry = registry
        self._agent_name = agent_name

    async def _wake(self, session_id: str, kind: str, text: str, **meta: Any) -> None:
        """The canonical wake-triple. ``session_id`` is the assignee/parent
        routing-key ``<transport>:<native_id>``; resolve the sibling session of
        THIS agent, deliver the OS message, and ensure its run-loop runs (booting
        a loopless A2A/MCP session; idempotent for a looped one)."""
        transport, _, native_id = session_id.partition(":")
        session = self._registry.resolve_session(self._agent_name, transport, native_id)
        await session._put_inbox(kind, {"text": text, "sender": "task:os", "meta": dict(meta)})
        self._registry.ensure_session_running(self._agent_name, session_id)

    async def wake_ready_dependent(self, task: Any) -> None:
        """Complete-side: a dependent the OS promoted to ``ready`` → wake it to
        continue. The session resumes the task's work via ordinary ops."""
        await self._wake(
            task.assignee, WAKE_READY_KIND,
            f"[task] Task {task.task_id!r} ('{task.name}') is now READY — its "
            f"dependencies are all satisfied. Continue its work.",
            task_id=task.task_id,
        )

    async def notify_parent_decide(self, *, parent_session: str, terminal_task: Any,
                                   dependents: "list[Any]", disposition: str | None = None) -> None:
        """Abort/failed/cap_exceeded-side: wake the parent to decide recovery for
        its stuck dependents (it re-wires via ordinary task ops — repoint to a
        substitute / remove the edge / fail / handle itself; P7 — no decision
        vocabulary). ``disposition`` is the first-class terminal reason (#1953
        slice 8 no-conflation: a budget ``cap_exceeded`` vs a genuine ``failed``)."""
        dep_ids = [d.task_id for d in dependents]
        disp = disposition or terminal_task.status.value
        await self._wake(
            parent_session, WAKE_PARENT_KIND,
            f"[task] A dependency of your sub-tasks reached {disp!r}: task "
            f"{terminal_task.task_id!r} ('{terminal_task.name}'). These dependents "
            f"are now stuck: {dep_ids}. Decide recovery using the task ops (repoint "
            f"to a substitute, remove the dependency, fail them, or handle the work "
            f"yourself).",
            task_id=terminal_task.task_id, dependents=dep_ids, disposition=disp,
        )


__all__ = ["TaskWaker", "WAKE_READY_KIND", "WAKE_PARENT_KIND"]
