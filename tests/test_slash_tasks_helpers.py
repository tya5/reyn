"""Tier 2: /tasks slash — _list_dynamic_task_lines paths.

`_list_dynamic_task_lines` is the async listing helper whose filter logic
(archived hidden, deps summary) needs pinning independently of the full /tasks
handler machinery.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.interfaces.slash.tasks import _list_dynamic_task_lines

# ── _list_dynamic_task_lines ───────────────────────────────────────────────


def _stub_task(
    name: str,
    task_id: str,
    status: str = "running",
    *,
    archived_at: object = None,
    deps: list[str] | None = None,
    description: str | None = None,
    assignee: str | None = None,
    result: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        task_id=task_id,
        status=SimpleNamespace(value=status),
        archived_at=archived_at,
        deps=deps or [],
        description=description,
        assignee=assignee,
        result=result,
    )


class _FakeBackend:
    def __init__(self, tasks: list) -> None:
        self._tasks = tasks

    async def list(self) -> list:
        return list(self._tasks)


class _FakeSession:
    def __init__(self, *, backend=None) -> None:
        self.task_backend = backend


@pytest.mark.asyncio
async def test_list_dynamic_no_backend_returns_empty() -> None:
    """Tier 2: session without task_backend → empty list + 0 done count (no crash)."""
    session = _FakeSession(backend=None)
    lines, done_count = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert lines == []
    assert done_count == 0


@pytest.mark.asyncio
async def test_list_dynamic_archived_tasks_hidden() -> None:
    """Tier 2: soft-deleted (archived_at set) tasks are excluded from listing."""
    archived = _stub_task("old", "task-aaa", archived_at="2026-01-01")
    active = _stub_task("active", "task-bbb")
    session = _FakeSession(backend=_FakeBackend([archived, active]))
    lines, done_count = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("active" in ln for ln in lines)
    assert not any("old" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_no_deps_shows_none() -> None:
    """Tier 2: task with no deps shows '(none)' in deps field."""
    task = _stub_task("t1", "task-ccc", deps=[])
    session = _FakeSession(backend=_FakeBackend([task]))
    lines, _ = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("(none)" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_deps_shown_truncated() -> None:
    """Tier 2: task with deps shows first 8 chars of each dep id."""
    task = _stub_task("t2", "task-ddd", deps=["dep-long-id-1", "dep-long-id-2"])
    session = _FakeSession(backend=_FakeBackend([task]))
    lines, _ = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("dep-long" in ln for ln in lines)
    assert not any("dep-long-id-1" in ln for ln in lines)  # truncated to 8 chars


@pytest.mark.asyncio
async def test_list_dynamic_status_shown() -> None:
    """Tier 2: task status value surfaces in the output line for non-done tasks."""
    task = _stub_task("t3", "task-eee", status="running")
    session = _FakeSession(backend=_FakeBackend([task]))
    lines, _ = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("running" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_empty_backend_returns_empty() -> None:
    """Tier 2: backend with no tasks → empty lines list + 0 done count."""
    session = _FakeSession(backend=_FakeBackend([]))
    lines, done_count = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert lines == []
    assert done_count == 0


@pytest.mark.asyncio
async def test_list_dynamic_done_tasks_folded_into_count() -> None:
    """Tier 2: DONE tasks are excluded from active lines and counted separately (#2040)."""
    active = _stub_task("active", "task-fff", status="running")
    done1 = _stub_task("done1", "task-ggg", status="done")
    done2 = _stub_task("done2", "task-hhh", status="done")
    session = _FakeSession(backend=_FakeBackend([active, done1, done2]))
    lines, done_count = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("active" in ln for ln in lines)
    assert not any("done1" in ln for ln in lines)
    assert not any("done2" in ln for ln in lines)
    assert done_count == 2


@pytest.mark.asyncio
async def test_list_dynamic_all_done_returns_empty_lines_nonzero_count() -> None:
    """Tier 2: all tasks DONE → empty active lines, nonzero done_count (#2040)."""
    done = _stub_task("step", "task-iii", status="done")
    session = _FakeSession(backend=_FakeBackend([done]))
    lines, done_count = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert lines == []
    assert done_count == 1
