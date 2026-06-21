"""A2A view of a reyn Task (#1953 slice 5a).

The boundary mapper from a reyn ``Task`` (term-neutral core, #1953) to an A2A
Task envelope. Lives in the A2A layer (P7): the Task core stays term-neutral; the
A2A vocabulary (``TaskState``, ``contextId``) is constructed only here. ``contextId``
is recovered from the assignee's core session_id via the A2A reverse map (#1814).

This replaces the closed #1948 RunEntry-based mapping with the settled Task
lifecycle vocabulary (H3).
"""
from __future__ import annotations

from reyn.runtime.a2a_routing import a2a_context_id
from reyn.task.model import TaskState

# reyn Task state → A2A TaskState (JSON-RPC short-form binding, consistent with
# the slash-form ``message/send`` Reyn ships).
#
# ⚠ ``blocked → input-required`` is an **interim placeholder** (slice 5 surfaces
# no blocked tasks yet). The **precise split lands in slice 7** with the
# block-reason discriminator: a **DAG-dependency** block maps to ``working`` (it
# resolves automatically — telling an A2A client ``input-required`` would be a
# lie), while only **external-input / auth** blocks map to
# ``input-required`` / ``auth-required`` (H3).
_TASK_STATE_TO_A2A: dict[str, str] = {
    "pending": "submitted",
    "ready": "submitted",
    "in_progress": "working",
    "blocked": "input-required",   # interim — see note above (slice 7)
    "completed": "completed",
    "failed": "failed",
    "aborted": "canceled",
    "archived": "canceled",        # abort = delete → archived → A2A canceled
}

# The legal A2A TaskState values this mapper may emit (drift guard for the
# completeness test — every map target must be one of these).
_A2A_TASK_STATES: frozenset[str] = frozenset({
    "submitted", "working", "input-required", "auth-required",
    "completed", "failed", "canceled",
})


def task_state_to_a2a(state: str) -> str:
    """Map a reyn Task state to an A2A TaskState. Unknown → ``working`` (a safe,
    non-terminal default — never silently report a non-spec state)."""
    return _TASK_STATE_TO_A2A.get(state, "working")


def to_a2a_task(task) -> dict:
    """Build a spec-shaped A2A Task envelope from a reyn ``Task``.

    ``id`` = task_id; ``status.state`` = the mapped A2A TaskState; ``contextId``
    is the A2A view of the assignee's core session_id (reverse map, #1814 — the
    contextId term stays in the A2A layer). ``kind: "task"`` is the JSON-RPC
    discriminator.
    """
    status_obj: dict = {
        "state": task_state_to_a2a(task.status.value),
        "timestamp": task.updated_at,
    }
    out: dict = {
        "kind": "task",
        "id": task.task_id,
        "status": status_obj,
    }
    if task.assignee:
        out["contextId"] = a2a_context_id(task.assignee)
    return out
