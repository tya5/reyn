"""A2A view of a ``RunEntry`` (#2839 Phase 1 — A2A decoupled from ``task_backend``).

The boundary mapper from a Reyn ``RunEntry`` (A2A's own flat run-registry
entry — see ``run_registry.py``) to an A2A Task envelope. Lives in the A2A
layer (P7): ``RunEntry`` stays term-neutral of A2A vocabulary; the A2A wire
terms (``TaskState`` strings, ``contextId``) are constructed only here.
``contextId`` is recovered from the run's core ``session_id`` via the A2A
reverse map (#1814).

This replaces the prior #1953-slice-5a mapping (a reyn ``Task``-backed
mapper — closed #1948's ``RunEntry``-based mapping had been retired in
favor of it). #2839 Phase 1 reverses that: A2A's GetTask / ListTasks /
Cancel authority moves back onto ``RunEntry`` because ``RunEntry`` already
carries every state A2A actually needs, INCLUDING ``input-required``
natively (the Task-vocab version had to overload ``blocked`` for it — an
interim placeholder the in-tree comment flagged as a known-incorrect "lie"
to the A2A client, since a DAG-dependency block is not the same thing as an
ask_user escalation; that placeholder never got its promised fix). Phase 1
therefore retires an unfixed correctness bug, not just a migration.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from reyn.interfaces.web.run_registry import RunStatus
from reyn.runtime.a2a_routing import a2a_context_id

if TYPE_CHECKING:
    from reyn.interfaces.web.run_registry import RunEntry

# RunEntry.status (the narrow #2839 Phase 1 run-status vocabulary) → A2A
# TaskState (JSON-RPC short-form binding, consistent with the slash-form
# ``message/send`` Reyn ships). Every RunStatus member is covered — this is
# the exhaustive Phase 1 replacement for the prior Task-state table.
_RUN_STATUS_TO_A2A: dict[RunStatus, str] = {
    RunStatus.RUNNING: "working",
    RunStatus.INPUT_REQUIRED: "input-required",
    RunStatus.COMPLETED: "completed",
    RunStatus.FAILED: "failed",
    RunStatus.CANCELLED: "canceled",
}

# The legal A2A TaskState values this mapper may emit (drift guard for the
# completeness test — every map target must be one of these).
_A2A_TASK_STATES: frozenset[str] = frozenset({
    "submitted", "working", "input-required", "auth-required",
    "completed", "failed", "canceled",
})


def run_status_to_a2a(status: RunStatus) -> str:
    """Map a ``RunEntry`` status to an A2A TaskState. Unknown → ``working``
    (a safe, non-terminal default — never silently report a non-spec state)."""
    return _RUN_STATUS_TO_A2A.get(status, "working")


def to_a2a_task(entry: "RunEntry") -> dict:
    """Build a spec-shaped A2A Task envelope from a ``RunEntry``.

    ``id`` = run_id (A2A's ``task_id``); ``status.state`` = the mapped A2A
    TaskState; ``contextId`` is the A2A view of the run's core ``session_id``
    (reverse map, #1814 — the contextId term stays in the A2A layer). A run
    with no ``session_id`` (pre-#1814 entries, or entries created without one)
    omits ``contextId`` rather than guessing.
    """
    status_obj: dict = {
        "state": run_status_to_a2a(entry.status),
        "timestamp": entry.updated_at.isoformat(),
    }
    out: dict = {
        "kind": "task",
        "id": entry.run_id,
        "status": status_obj,
    }
    if entry.session_id:
        out["contextId"] = a2a_context_id(entry.session_id)
    return out


__all__ = ["to_a2a_task", "run_status_to_a2a"]
