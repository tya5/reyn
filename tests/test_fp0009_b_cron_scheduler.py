"""Tests for FP-0009 Component B — CronScheduler (src/reyn/cron/).

All tests are Tier 1 (public API contract) or Tier 2b (subsystem invariant).
Docstrings declare tier on first line per policy.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from reyn.cron import CronJob, CronScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Injectable clock that tests can advance manually."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._now = dt


async def _wait_for_calls(runner: "_RecordingRunner", min_calls: int = 1, step: float = 0.05) -> None:
    """Busy-poll until runner has at least ``min_calls`` calls recorded."""
    while len(runner.calls) < min_calls:
        await asyncio.sleep(step)


class _RecordingRunner:
    """Real async callable that records each job fire."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: List[CronJob] = []
        self._raise = raise_exc

    async def __call__(self, job: CronJob) -> str:
        self.calls.append(job)
        if self._raise is not None:
            raise self._raise
        return "ok"


def _job(name: str = "j1", schedule: str = "* * * * *", **kwargs) -> CronJob:
    return CronJob(name=name, skill="test_skill", schedule=schedule, **kwargs)


# ---------------------------------------------------------------------------
# 1. CronJob construction + to_dict
# ---------------------------------------------------------------------------


def test_cronjob_constructs_with_required_fields():
    """Tier 1: CronJob builds with name/skill/schedule; optional fields default correctly."""
    job = CronJob(name="my-job", skill="my_skill", schedule="0 * * * *")
    assert job.name == "my-job"
    assert job.skill == "my_skill"
    assert job.schedule == "0 * * * *"
    assert job.input == {}
    assert job.enabled is True
    assert job.last_run_at is None
    assert job.last_run_status is None
    assert job.last_run_error is None
    assert job.next_run_at is None
    assert job.last_run_duration_seconds is None


def test_cronjob_to_dict_is_json_serialisable():
    """Tier 1: to_dict returns a dict with no datetime objects (all ISO strings or None)."""
    import json

    job = CronJob(name="j", skill="s", schedule="0 * * * *")
    d = job.to_dict()
    # Must round-trip through JSON without error
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["name"] == "j"
    assert decoded["skill"] == "s"
    assert decoded["schedule"] == "0 * * * *"
    assert decoded["enabled"] is True
    assert decoded["last_run_at"] is None
    assert decoded["last_run_status"] is None
    assert decoded["last_run_error"] is None
    assert decoded["next_run_at"] is None
    assert decoded["last_run_duration_seconds"] is None
    assert decoded["input"] == {}


def test_cronjob_to_dict_serialises_datetime_fields():
    """Tier 1: to_dict converts datetime fields to ISO strings."""
    import json

    now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    job = CronJob(name="j", skill="s", schedule="* * * * *")
    job.last_run_at = now
    job.next_run_at = now
    job.last_run_status = "ok"
    job.last_run_duration_seconds = 1.23

    d = job.to_dict()
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["last_run_at"] == now.isoformat()
    assert decoded["next_run_at"] == now.isoformat()
    assert decoded["last_run_status"] == "ok"
    assert decoded["last_run_duration_seconds"] == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# 2. compute_next_run — valid expression
# ---------------------------------------------------------------------------


def test_compute_next_run_returns_future_datetime():
    """Tier 1: compute_next_run returns a datetime in the future for a valid cron expr."""
    clock = _FakeClock()
    scheduler = CronScheduler([], clock_fn=clock)
    job = _job(schedule="0 */6 * * *")

    next_run = scheduler.compute_next_run(job)

    assert next_run is not None
    assert isinstance(next_run, datetime)
    assert next_run > clock()
    assert job.enabled is True  # not disabled


# ---------------------------------------------------------------------------
# 3. compute_next_run — invalid expression
# ---------------------------------------------------------------------------


def test_compute_next_run_invalid_disables_job_and_returns_none(caplog):
    """Tier 1: compute_next_run with invalid cron expr returns None, disables job, logs WARNING."""
    import logging

    clock = _FakeClock()
    scheduler = CronScheduler([], clock_fn=clock)
    job = _job(schedule="not-a-cron-expression")

    with caplog.at_level(logging.WARNING, logger="reyn.cron.scheduler"):
        result = scheduler.compute_next_run(job)

    assert result is None
    assert job.enabled is False
    assert any("invalid schedule" in r.message or "invalid" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. start() spawns tasks only for enabled jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_spawns_tasks_for_enabled_jobs_only():
    """Tier 2b: start() creates one task per enabled job; disabled jobs have no task."""
    runner = _RecordingRunner()
    jobs = [
        _job(name="enabled1", schedule="0 * * * *"),
        _job(name="enabled2", schedule="0 0 * * *"),
        _job(name="disabled", schedule="* * * * *", enabled=False),
    ]
    sched = CronScheduler(jobs, runner_fn=runner)

    await sched.start()
    try:
        # Give tasks a moment to initialise without firing (schedules are hours away)
        await asyncio.sleep(0.01)
        task_names = set(sched._tasks.keys())
        assert "enabled1" in task_names
        assert "enabled2" in task_names
        assert "disabled" not in task_names
    finally:
        await sched.stop()


# ---------------------------------------------------------------------------
# 5. stop() cancels all tasks; idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_all_tasks():
    """Tier 2b: stop() cancels all running tasks cleanly."""
    runner = _RecordingRunner()
    jobs = [_job(name="j1", schedule="0 * * * *"), _job(name="j2", schedule="0 0 * * *")]
    sched = CronScheduler(jobs, runner_fn=runner)

    await sched.start()
    await asyncio.sleep(0.01)

    await sched.stop()

    # After stop, no tasks remain in the dict
    assert sched._tasks == {}
    assert sched._running is False


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    """Tier 2b: calling stop() twice does not raise."""
    runner = _RecordingRunner()
    sched = CronScheduler([_job()], runner_fn=runner)
    await sched.start()
    await sched.stop()
    # Second stop must not raise
    await sched.stop()


# ---------------------------------------------------------------------------
# 6. run_now — invokes runner, updates fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_now_invokes_runner_and_updates_fields():
    """Tier 1: run_now triggers runner, returns True, updates last_run_at/status."""
    runner = _RecordingRunner()
    job = _job(name="target")
    sched = CronScheduler([job], runner_fn=runner)

    result = await sched.run_now("target")

    assert result is True
    assert runner.calls
    assert runner.calls[0] is job
    assert job.last_run_at is not None
    assert job.last_run_status == "ok"
    assert job.last_run_error is None
    assert job.last_run_duration_seconds is not None


# ---------------------------------------------------------------------------
# 7. run_now — unknown name returns False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_now_returns_false_for_unknown_name():
    """Tier 1: run_now returns False when the job name doesn't exist."""
    runner = _RecordingRunner()
    sched = CronScheduler([], runner_fn=runner)

    result = await sched.run_now("nonexistent")

    assert result is False
    assert runner.calls == []


# ---------------------------------------------------------------------------
# 8. run_now — returns False when runner is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_now_returns_false_when_runner_is_none():
    """Tier 1: run_now returns False when no runner is configured."""
    job = _job(name="j")
    sched = CronScheduler([job])  # no runner_fn

    result = await sched.run_now("j")

    assert result is False


# ---------------------------------------------------------------------------
# 9. Periodic fire with controlled clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_fire_with_controlled_clock():
    """Tier 2b: job fires based on croniter-computed next time using a real short interval.

    Uses "* * * * *" (every minute). The fake clock starts 59 seconds past a
    minute boundary so the next computed fire time is ~1 real second away.
    We wait up to 3 real seconds for the runner to be called, confirming the
    scheduler correctly sleeps for the computed interval then fires.

    This test verifies the core scheduling loop without mocking asyncio.sleep
    (which is forbidden by the testing policy). The 1-second real wait is
    intentional — it is the minimal observable interval given a minute-cron.
    """
    # 0:00:59 UTC — next "* * * * *" fire is at 0:01:00 (1 real second away)
    start = datetime(2026, 1, 1, 0, 0, 59, tzinfo=timezone.utc)
    clock = _FakeClock(start)
    runner = _RecordingRunner()
    job = _job(name="periodic", schedule="* * * * *")
    sched = CronScheduler([job], clock_fn=clock, runner_fn=runner)

    # Verify croniter agrees on the next fire time
    next_at = sched.compute_next_run(job)
    assert next_at is not None
    # clock() = 0:00:59, next_at = 0:01:00 — 1 second wait
    wait_secs = (next_at - clock()).total_seconds()
    assert 0 < wait_secs <= 2, f"expected ~1s wait, got {wait_secs}"

    await sched.start()
    try:
        # Wait up to 3 seconds (generous for CI); the scheduler sleeps ~1s then fires
        await asyncio.wait_for(
            _wait_for_calls(runner, min_calls=1),
            timeout=3.0,
        )
        assert len(runner.calls) >= 1, "runner was never called"
        assert runner.calls[0] is job
        assert job.last_run_status == "ok"
    finally:
        await sched.stop()


# ---------------------------------------------------------------------------
# 10. Runner exception → error status, scheduler continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_exception_records_error_and_scheduler_continues():
    """Tier 2b: runner exception sets last_run_status=error, error captured; loop continues.

    Uses the same real 1-second fire approach as test_periodic_fire_with_controlled_clock.
    Clock starts at 0:00:59 so the next "* * * * *" fire is ~1 real second away.
    """
    start = datetime(2026, 1, 1, 0, 0, 59, tzinfo=timezone.utc)
    clock = _FakeClock(start)
    runner = _RecordingRunner(raise_exc=RuntimeError("boom"))
    job = _job(name="failing", schedule="* * * * *")
    sched = CronScheduler([job], clock_fn=clock, runner_fn=runner)

    await sched.start()
    try:
        await asyncio.wait_for(
            _wait_for_calls(runner, min_calls=1),
            timeout=3.0,
        )
        assert len(runner.calls) >= 1, "runner was never called within timeout"
        assert job.last_run_status == "error"
        assert job.last_run_error is not None
        assert "boom" in job.last_run_error
    finally:
        await sched.stop()


# ---------------------------------------------------------------------------
# 11. set_runner after construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_runner_after_construction():
    """Tier 1: set_runner() injects runner after construction; run_now uses it."""
    job = _job(name="j")
    sched = CronScheduler([job])  # no runner initially
    assert await sched.run_now("j") is False  # no runner → False

    runner = _RecordingRunner()
    sched.set_runner(runner)
    result = await sched.run_now("j")

    assert result is True
    assert runner.calls
    assert job.last_run_status == "ok"


# ---------------------------------------------------------------------------
# 12. start() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent():
    """Tier 2b: calling start() twice does not double-spawn tasks."""
    runner = _RecordingRunner()
    job = _job(name="j", schedule="0 * * * *")
    sched = CronScheduler([job], runner_fn=runner)

    await sched.start()
    task_after_first = dict(sched._tasks)

    await sched.start()  # second call must be no-op
    task_after_second = dict(sched._tasks)

    # Same task objects — no duplication
    assert set(task_after_first.keys()) == set(task_after_second.keys())
    for name in task_after_first:
        assert task_after_first[name] is task_after_second[name]

    await sched.stop()


# ---------------------------------------------------------------------------
# 13. jobs() + get_job() accessors
# ---------------------------------------------------------------------------


def test_jobs_returns_all_jobs():
    """Tier 1: jobs() returns all jobs including disabled ones in insertion order."""
    j1 = _job(name="a")
    j2 = _job(name="b", enabled=False)
    j3 = _job(name="c")
    sched = CronScheduler([j1, j2, j3])

    result = sched.jobs()

    assert [j.name for j in result] == ["a", "b", "c"]


def test_get_job_returns_job_or_none():
    """Tier 1: get_job returns the job by name or None for unknown names."""
    job = _job(name="known")
    sched = CronScheduler([job])

    assert sched.get_job("known") is job
    assert sched.get_job("unknown") is None


# ---------------------------------------------------------------------------
# 14. _fire with no runner logs WARNING + sets error status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_without_runner_marks_error(caplog):
    """Tier 2b: firing a job without a runner marks status=error and logs WARNING."""
    import logging

    job = _job(name="j")
    sched = CronScheduler([job])  # no runner

    with caplog.at_level(logging.WARNING, logger="reyn.cron.scheduler"):
        await sched._fire(job)

    assert job.last_run_status == "error"
    assert job.last_run_error == "no runner configured"
    assert any("no runner" in r.message.lower() for r in caplog.records)
