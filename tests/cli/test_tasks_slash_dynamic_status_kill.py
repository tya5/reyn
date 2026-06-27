"""Tier 2: /tasks status|kill resolve + act on dynamic tasks (#2036 follow-up).

Dogfood-found (live, real-terminal): /tasks (#2036) LISTS the dynamic tasks the
chat LLM creates via ``task__create``, but ``_resolve_task`` matched only
``running_skills`` — so ``/tasks status <task_id>`` / ``/tasks kill <task_id>``
said "no task matches" for a task the list had just shown (the list-vs-action
gap). These pin that status renders the dynamic task + kill aborts it.

No mocks — a real :class:`InMemoryTaskBackend` + a Session-shaped capture
exposing the public surface the handlers read (``task_backend`` /
``running_skills`` / ``get_skill_registry``) + the ``_put_outbox`` reply seam.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash.tasks import _kill_task, _task_status
from reyn.task import InMemoryTaskBackend, Task, TaskState


class _CaptureSession:
    """Session-shaped stand-in capturing the reply text the handlers emit."""

    def __init__(self, *, running_skills=None, task_backend=None):
        self.running_skills = running_skills or {}
        self._task_backend = task_backend
        self.replies: list[str] = []

    @property
    def task_backend(self):
        return self._task_backend

    def get_skill_registry(self):
        return None

    async def _put_outbox(self, message) -> None:
        self.replies.append(message.text)


async def _seed_task(backend: InMemoryTaskBackend) -> str:
    task = Task(
        task_id="task-aaaaaaaa-0001",
        name="ingest_raw_csv",
        assignee="sess-1",
        requester="req",
        status=TaskState.RUNNING,
    )
    await backend.create(task)
    return task.task_id


@pytest.mark.asyncio
async def test_tasks_status_resolves_dynamic_task() -> None:
    """Tier 2: ``/tasks status <dynamic_task_id>`` renders the task — not the
    pre-fix "no task matches" (which fired because _resolve_task only knew skill
    runs)."""
    backend = InMemoryTaskBackend()
    await _seed_task(backend)
    session = _CaptureSession(task_backend=backend)

    await _task_status(session, "task-aaa")

    out = session.replies[-1]
    assert "no task matches" not in out
    assert "ingest_raw_csv" in out
    assert "running" in out


@pytest.mark.asyncio
async def test_tasks_kill_aborts_dynamic_task() -> None:
    """Tier 2: ``/tasks kill <dynamic_task_id>`` aborts the task via the backend."""
    backend = InMemoryTaskBackend()
    task_id = await _seed_task(backend)
    session = _CaptureSession(task_backend=backend)

    await _kill_task(session, "task-aaa")

    assert "aborted" in session.replies[-1].lower()
    # The real backend transitioned the task out of the runnable set.
    task = await backend.get(task_id)
    assert task is not None
    assert getattr(task.status, "value", task.status) == "aborted"


@pytest.mark.asyncio
async def test_tasks_status_unmatched_dynamic_prefix_reports_no_match() -> None:
    """Tier 2: a prefix matching neither a skill run nor a dynamic task still
    reports "no task matches" (the resolver spans both families)."""
    backend = InMemoryTaskBackend()
    await _seed_task(backend)
    session = _CaptureSession(task_backend=backend)

    await _task_status(session, "zzzzzzzz")

    assert "no task matches" in session.replies[-1]
