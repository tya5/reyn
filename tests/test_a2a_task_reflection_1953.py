"""Tier 2: #1953 slice 5b — A2A async-run terminal status-reflection.

The async background run reflects its terminal outcome onto the canonical Task
via the single-writer assignee CAS (``_reflect_task_status``). The reflection
is **best-effort + race-safe**: if an A2A ``Cancel`` archived the Task first, the
terminal-guard rejects the late reflection and the abort wins — the helper
swallows the rejection so the background run never crashes on it.

Real Task backend, no mocks. Observed via the backend's public ``get``.

Falsification:
- (a) a completed run reflects ``completed`` onto an in_progress Task (RED if the
  reflection is dropped — the Task would stay in_progress, a stale tombstone);
- (b) a reflection after the requester aborted is swallowed and the Task stays
  archived (RED if the terminal-guard rejection re-raises out of the helper, or
  if the late write clobbers the abort).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.web.routers.a2a import _reflect_task_status
from reyn.runtime.a2a_routing import a2a_session_id
from reyn.task import InMemoryTaskBackend, Task, TaskOrigin, TaskState


def _a2a_task(backend, task_id, sid):
    return backend.create(
        Task(task_id=task_id, name="n", assignee=sid, requester="external",
             origin=TaskOrigin.EXTERNAL, status=TaskState.IN_PROGRESS)
    )


@pytest.mark.asyncio
async def test_completed_run_reflects_onto_task():
    """Tier 2: (a) a completed async run reflects ``completed`` onto its Task via
    the assignee CAS (the run executes on the assignee session)."""
    backend = InMemoryTaskBackend()
    sid = a2a_session_id("ctx-r")
    await _a2a_task(backend, "t-1", sid)

    await _reflect_task_status(backend, "t-1", "completed")

    refreshed = await backend.get("t-1")
    assert refreshed is not None and refreshed.status is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_reflection_after_abort_is_swallowed_and_abort_wins():
    """Tier 2: (b) when an A2A Cancel archived the Task first, the late terminal
    reflection is rejected by the terminal-guard, swallowed (no raise), and the
    archived (abort) state stands — the race-safe contract lead affirmed."""
    backend = InMemoryTaskBackend()
    sid = a2a_session_id("ctx-r")
    await _a2a_task(backend, "t-1", sid)

    aborted = await backend.abort("t-1")  # requester cancels first → archived
    assert aborted and aborted[0].status is TaskState.ARCHIVED

    # Must NOT raise even though the Task is terminal (best-effort reflection).
    await _reflect_task_status(backend, "t-1", "completed")

    refreshed = await backend.get("t-1")
    assert refreshed is not None and refreshed.status is TaskState.ARCHIVED
