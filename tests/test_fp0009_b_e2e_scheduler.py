"""End-to-end test for FP-0009 Component B — cron-driven skill scheduling.

Tier 3: Demonstrates the full pipeline from reyn.yaml config loading through
CronScheduler construction and job execution, verifying side-effects via a
recording runner rather than a live LLM invocation.

The test does NOT invoke Agent.run (= B4's lifespan integration owns that
sanity check). The purpose here is to verify the
config→CronJob→CronScheduler→runner pipeline end-to-end without LLM.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from reyn.config import CronConfig, CronJobConfig, _build_cron_config, load_config
from reyn.cron import CronJob as CronJobRuntime
from reyn.cron import CronScheduler

# ---------------------------------------------------------------------------
# Recording runner — real async callable, no mocks
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Real async callable that records each job fire for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (skill, input)

    async def __call__(self, job: CronJobRuntime) -> str:
        self.calls.append((job.skill, dict(job.input)))
        return "ok"


# ---------------------------------------------------------------------------
# Helper: write a minimal reyn.yaml with one cron job
# ---------------------------------------------------------------------------


def _write_reyn_yaml(path: Path, content: dict) -> None:
    path.write_text(
        yaml.dump(content, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cron_config_parsed_from_reyn_yaml(tmp_path: Path) -> None:
    """Tier 3: CronConfig round-trips correctly from a reyn.yaml cron block.

    Verifies that _build_cron_config and load_config both produce the
    expected CronJobConfig entries when a cron block is present in reyn.yaml.
    """
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_reyn_yaml(
        reyn_yaml,
        {
            "model": "standard",
            "cron": {
                "jobs": [
                    {
                        "name": "index_events_hourly",
                        "skill": "index_events",
                        "schedule": "0 */6 * * *",
                        "input": {},
                        "enabled": True,
                    },
                    {
                        "name": "weekly_ops_report",
                        "skill": "ops_report",
                        "schedule": "0 9 * * MON",
                        "input": {"since_days": 7},
                        "enabled": True,
                    },
                ]
            },
        },
    )

    cfg = load_config(tmp_path)

    assert cfg.cron.jobs

    job0 = cfg.cron.jobs[0]
    assert job0.name == "index_events_hourly"
    assert job0.skill == "index_events"
    assert job0.schedule == "0 */6 * * *"
    assert job0.input == {}
    assert job0.enabled is True

    job1 = cfg.cron.jobs[1]
    assert job1.name == "weekly_ops_report"
    assert job1.skill == "ops_report"
    assert job1.schedule == "0 9 * * MON"
    assert job1.input == {"since_days": 7}
    assert job1.enabled is True


def test_cron_config_absent_yields_empty_jobs(tmp_path: Path) -> None:
    """Tier 3: Missing cron block in reyn.yaml → CronConfig with no jobs."""
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_reyn_yaml(reyn_yaml, {"model": "standard"})

    cfg = load_config(tmp_path)

    assert cfg.cron.jobs == []


@pytest.mark.asyncio
async def test_scheduler_built_from_config_jobs_and_run_now(tmp_path: Path) -> None:
    """Tier 3: Full pipeline — reyn.yaml → CronConfig → CronJob list → CronScheduler
    → run_now fires the runner with the right skill and input.

    Steps:
    1. Write reyn.yaml with one cron job
    2. Load config via load_config
    3. Convert CronJobConfig entries to CronJob runtime objects
    4. Build CronScheduler with a recording runner
    5. start() → assert jobs() count
    6. run_now("test_job") → assert runner recorded the correct skill+input
    7. Assert last_run_at is set and last_run_status == "ok"
    8. stop() → assert graceful shutdown
    """
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_reyn_yaml(
        reyn_yaml,
        {
            "model": "standard",
            "cron": {
                "jobs": [
                    {
                        "name": "test_job",
                        "skill": "index_events",
                        "schedule": "* * * * *",
                        "input": {"since": "2026-01-01T00:00:00"},
                        "enabled": True,
                    }
                ]
            },
        },
    )

    # Step 2: load config
    cfg = load_config(tmp_path)
    assert cfg.cron.jobs

    # Step 3: convert CronJobConfig → CronJob runtime objects
    runtime_jobs = [
        CronJobRuntime(
            name=jc.name,
            skill=jc.skill,
            schedule=jc.schedule,
            input=jc.input,
            enabled=jc.enabled,
        )
        for jc in cfg.cron.jobs
    ]

    # Step 4: build scheduler with recording runner
    runner = _RecordingRunner()
    scheduler = CronScheduler(runtime_jobs, runner_fn=runner)

    # Step 5: start and verify job count
    await scheduler.start()
    try:
        assert scheduler.jobs()
        assert scheduler.jobs()[0].name == "test_job"

        # Step 6: run_now triggers the runner
        result = await scheduler.run_now("test_job")
        assert result is True

        assert runner.calls
        fired_skill, fired_input = runner.calls[0]
        assert fired_skill == "index_events"
        assert fired_input == {"since": "2026-01-01T00:00:00"}

        # Step 7: last_run_* fields updated
        job = scheduler.get_job("test_job")
        assert job is not None
        assert job.last_run_at is not None
        assert isinstance(job.last_run_at, datetime)
        assert job.last_run_status == "ok"
        assert job.last_run_error is None
        assert job.last_run_duration_seconds is not None

    finally:
        # Step 8: graceful shutdown
        await scheduler.stop()

    # After stop: no tasks remain, running flag cleared
    assert scheduler.tasks == {}
    assert scheduler.running is False


@pytest.mark.asyncio
async def test_disabled_job_not_scheduled(tmp_path: Path) -> None:
    """Tier 3: A disabled job in reyn.yaml is loaded into config but not spawned
    as an asyncio task when the scheduler starts.
    """
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_reyn_yaml(
        reyn_yaml,
        {
            "model": "standard",
            "cron": {
                "jobs": [
                    {
                        "name": "active_job",
                        "skill": "index_events",
                        "schedule": "0 * * * *",
                        "enabled": True,
                    },
                    {
                        "name": "disabled_job",
                        "skill": "ops_report",
                        "schedule": "0 * * * *",
                        "enabled": False,
                    },
                ]
            },
        },
    )

    cfg = load_config(tmp_path)
    job_names = {j.name for j in cfg.cron.jobs}
    assert "active_job" in job_names and "disabled_job" in job_names  # both loaded

    runner = _RecordingRunner()
    runtime_jobs = [
        CronJobRuntime(
            name=jc.name,
            skill=jc.skill,
            schedule=jc.schedule,
            input=jc.input,
            enabled=jc.enabled,
        )
        for jc in cfg.cron.jobs
    ]
    scheduler = CronScheduler(runtime_jobs, runner_fn=runner)

    await scheduler.start()
    try:
        # Only active_job gets a task; disabled_job does not
        task_names = set(scheduler.tasks.keys())
        assert "active_job" in task_names
        assert "disabled_job" not in task_names

        # jobs() accessor returns all (including disabled)
        all_job_names = [j.name for j in scheduler.jobs()]
        assert "active_job" in all_job_names
        assert "disabled_job" in all_job_names

    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_run_now_returns_false_for_unknown_job(tmp_path: Path) -> None:
    """Tier 3: run_now with a job name not in the scheduler returns False without
    triggering the runner.
    """
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_reyn_yaml(
        reyn_yaml,
        {
            "model": "standard",
            "cron": {
                "jobs": [
                    {
                        "name": "real_job",
                        "skill": "index_events",
                        "schedule": "* * * * *",
                        "enabled": True,
                    }
                ]
            },
        },
    )

    cfg = load_config(tmp_path)
    runner = _RecordingRunner()
    runtime_jobs = [
        CronJobRuntime(
            name=jc.name,
            skill=jc.skill,
            schedule=jc.schedule,
            input=jc.input,
            enabled=jc.enabled,
        )
        for jc in cfg.cron.jobs
    ]
    scheduler = CronScheduler(runtime_jobs, runner_fn=runner)

    await scheduler.start()
    try:
        result = await scheduler.run_now("nonexistent_job")
        assert result is False
        assert runner.calls == []
    finally:
        await scheduler.stop()


def test_build_cron_config_directly() -> None:
    """Tier 3: _build_cron_config correctly parses a raw dict without going through
    the full load_config stack — exercises the parser in isolation.
    """
    raw = {
        "jobs": [
            {
                "name": "hourly_index",
                "skill": "index_events",
                "schedule": "0 * * * *",
                "input": {},
                "enabled": True,
            },
            {
                "name": "daily_report",
                "skill": "ops_report",
                "schedule": "0 8 * * *",
                # no input key — should default to {}
                # no enabled key — should default to True
            },
        ]
    }

    result = _build_cron_config(raw)

    assert isinstance(result, CronConfig)
    assert result.jobs

    assert result.jobs[0].name == "hourly_index"
    assert result.jobs[0].input == {}
    assert result.jobs[0].enabled is True

    assert result.jobs[1].name == "daily_report"
    assert result.jobs[1].skill == "ops_report"
    assert result.jobs[1].input == {}
    assert result.jobs[1].enabled is True
