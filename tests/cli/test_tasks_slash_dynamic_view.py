"""Tier 2: /tasks list spans skill runs + dynamic tasks (#2035).

Pins the unified-view contract for the ``/tasks`` slash command: its list
path renders BOTH the existing skill-run lines AND the dynamic Tasks the
chat LLM creates via ``task__create`` (read from ``session.task_backend``).

No mocks — a real :class:`InMemoryTaskBackend` is seeded with Tasks (with a
real dependency edge), and a minimal Session-shaped capture object exposes
the same public surface ``/tasks`` reads (``running_skills`` /
``get_skill_registry`` / ``task_backend``) plus the ``_put_outbox`` reply
seam. Per testing.md: real backend instance, public surface only, no
private-state assertions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash.tasks import _list_tasks
from reyn.task import InMemoryTaskBackend, Task, TaskState


class _CaptureSession:
    """Session-shaped stand-in capturing the reply text /tasks emits.

    Exposes only the public attributes ``/tasks`` reads. ``_put_outbox`` is
    the documented reply seam (``reply()`` -> ``session._put_outbox``); we
    record the emitted ``OutboxMessage.text`` so the test asserts on the
    rendered output the user sees.
    """

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


async def _seed_tasks(backend: InMemoryTaskBackend) -> None:
    """Two dynamic tasks with a real dependency: ``build`` -> depends on ``plan``.

    ``plan`` is IN_PROGRESS; ``build`` is born with a dep on the not-yet-completed
    ``plan`` so the backend derives it as BLOCKED (both non-terminal → both shown).
    """
    plan = Task(
        task_id="plan-aaaaaaaa-0001",
        name="plan",
        assignee="sess-1",
        requester="req",
        status=TaskState.IN_PROGRESS,
    )
    await backend.create(plan)
    build = Task(
        task_id="build-bbbbbbbb-0002",
        name="build",
        assignee="sess-1",
        requester="req",
        status=TaskState.READY,
        deps=["plan-aaaaaaaa-0001"],
    )
    await backend.create(build)


@pytest.mark.asyncio
async def test_tasks_list_includes_dynamic_tasks():
    """Tier 2: ``/tasks`` renders dynamic-task lines (name + short id + status)."""
    backend = InMemoryTaskBackend()
    await _seed_tasks(backend)
    session = _CaptureSession(task_backend=backend)

    await _list_tasks(session)

    assert session.replies, "expected /tasks to emit a reply"
    out = session.replies[-1]
    # Section header distinguishing dynamic tasks from skill runs.
    assert "Tasks:" in out
    # Each dynamic task surfaces name + short (8-char) id + status.
    assert "plan" in out
    assert "plan-aaa" in out  # task_id[:8]
    assert "status: in_progress" in out
    assert "build" in out
    assert "build-bb" in out  # task_id[:8]
    # The dependency edge is summarised by the depended-on short id.
    assert "deps: plan-aaa" in out


@pytest.mark.asyncio
async def test_tasks_list_shows_both_skill_runs_and_dynamic_tasks():
    """Tier 2: both sections render when skill runs AND dynamic tasks exist."""
    backend = InMemoryTaskBackend()
    await _seed_tasks(backend)
    session = _CaptureSession(
        running_skills={"run-skill-xyz": object()},
        task_backend=backend,
    )

    await _list_tasks(session)

    out = session.replies[-1]
    # Skill-run section still present (existing behavior preserved).
    assert "Skills:" in out
    assert "run-skill-xyz" in out
    # Dynamic-task section added alongside it.
    assert "Tasks:" in out
    assert "build" in out


@pytest.mark.asyncio
async def test_tasks_list_no_backend_falls_back_to_skill_only():
    """Tier 2: ``task_backend`` is None → no dynamic section, skill runs only."""
    session = _CaptureSession(
        running_skills={"run-skill-xyz": object()},
        task_backend=None,
    )

    await _list_tasks(session)

    out = session.replies[-1]
    assert "Skills:" in out
    assert "run-skill-xyz" in out
    assert "Tasks:" not in out


@pytest.mark.asyncio
async def test_tasks_list_empty_when_nothing_running():
    """Tier 2: no skill runs and no dynamic tasks → existing empty message."""
    session = _CaptureSession(task_backend=InMemoryTaskBackend())

    await _list_tasks(session)

    assert session.replies[-1] == "(no running tasks)"
