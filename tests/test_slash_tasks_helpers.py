"""Tier 2: /tasks slash — _format_elapsed pure helper + _list_dynamic_task_lines paths.

`_format_elapsed` is a pure duration formatter; `_list_dynamic_task_lines` is the
async listing helper whose filter logic (archived hidden, deps summary) needs pinning
independently of the full /tasks handler machinery.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.interfaces.slash.tasks import _format_elapsed, _list_dynamic_task_lines

# ── _format_elapsed ────────────────────────────────────────────────────────


def test_format_elapsed_seconds_range() -> None:
    """Tier 2: values < 60 render as '…s'."""
    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(1) == "1s"
    assert _format_elapsed(59) == "59s"


def test_format_elapsed_exactly_60_is_minutes() -> None:
    """Tier 2: exactly 60 seconds renders as '1m 00s'."""
    assert _format_elapsed(60) == "1m 00s"


def test_format_elapsed_minutes_range() -> None:
    """Tier 2: 61–3599 seconds render as '…m …s'."""
    assert _format_elapsed(90) == "1m 30s"
    assert _format_elapsed(3599) == "59m 59s"


def test_format_elapsed_exactly_1h_is_hours() -> None:
    """Tier 2: exactly 3600 seconds renders as '1h 00m'."""
    assert _format_elapsed(3600) == "1h 00m"


def test_format_elapsed_hours_range() -> None:
    """Tier 2: ≥ 3600 seconds render as '…h …m'."""
    assert _format_elapsed(3661) == "1h 01m"
    assert _format_elapsed(7200) == "2h 00m"
    assert _format_elapsed(7322) == "2h 02m"


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
    """Tier 2: session without task_backend → empty list (no crash)."""
    session = _FakeSession(backend=None)
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert lines == []


@pytest.mark.asyncio
async def test_list_dynamic_archived_tasks_hidden() -> None:
    """Tier 2: soft-deleted (archived_at set) tasks are excluded from listing."""
    archived = _stub_task("old", "task-aaa", archived_at="2026-01-01")
    active = _stub_task("active", "task-bbb")
    session = _FakeSession(backend=_FakeBackend([archived, active]))
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("active" in ln for ln in lines)
    assert not any("old" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_no_deps_shows_none() -> None:
    """Tier 2: task with no deps shows '(none)' in deps field."""
    task = _stub_task("t1", "task-ccc", deps=[])
    session = _FakeSession(backend=_FakeBackend([task]))
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("(none)" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_deps_shown_truncated() -> None:
    """Tier 2: task with deps shows first 8 chars of each dep id."""
    task = _stub_task("t2", "task-ddd", deps=["dep-long-id-1", "dep-long-id-2"])
    session = _FakeSession(backend=_FakeBackend([task]))
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("dep-long" in ln for ln in lines)
    assert not any("dep-long-id-1" in ln for ln in lines)  # truncated to 8 chars


@pytest.mark.asyncio
async def test_list_dynamic_status_shown() -> None:
    """Tier 2: task status value surfaces in the output line."""
    task = _stub_task("t3", "task-eee", status="completed")
    session = _FakeSession(backend=_FakeBackend([task]))
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert any("completed" in ln for ln in lines)


@pytest.mark.asyncio
async def test_list_dynamic_empty_backend_returns_empty() -> None:
    """Tier 2: backend with no tasks → empty lines list."""
    session = _FakeSession(backend=_FakeBackend([]))
    lines = await _list_dynamic_task_lines(session)  # type: ignore[arg-type]
    assert lines == []
