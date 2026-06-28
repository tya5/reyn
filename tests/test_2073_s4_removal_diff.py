"""Tier 2: #2073 S4 — hardening: the cron removal-diff (+ MCP removal / atomicity).

S2's cron seam was add/change-only — a job removed from .reyn/cron.yaml stayed
scheduled. S4 adds a removal-diff: the cron seam unschedules RUNTIME jobs deleted
from the file (tracked via self._runtime_cron_names, seeded at boot, updated each
reload) WITHOUT touching startup (reyn.yaml) jobs — the same startup/runtime layering
as hooks. (MCP removal is handled by the re-probe rebuilding the cache from the
current config; the hooks seam already handles removal via rebuild-from-scratch;
atomicity is the S2 validate-before-apply whole-reload reject.)

No mocks: a real Session (for the runtime-name tracking + the seam) + a real
CronScheduler; behavior via the public get_job.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.cron import CronJob, CronScheduler, set_active_scheduler
from reyn.runtime.session import Session


@pytest.fixture(autouse=True)
def _reset_active_scheduler():
    yield
    set_active_scheduler(None)


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="s4-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def _job(name: str) -> CronJob:
    return CronJob(name=name, schedule="* * * * *", to="x", message="m")


def _jd(name: str) -> dict:
    return {"name": name, "schedule": "* * * * *", "to": "x", "message": "m"}


@pytest.mark.asyncio
async def test_cron_removal_diff_unschedules_removed_runtime_job(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a runtime job removed from .reyn/cron.yaml is unscheduled on reload —
    while a STARTUP (reyn.yaml) job, never tracked as runtime, is left scheduled."""
    monkeypatch.chdir(tmp_path)
    sched = CronScheduler([_job("startup_job")])  # a pre-loaded startup job
    set_active_scheduler(sched)
    session = _make_session(tmp_path)  # no .reyn/cron.yaml at boot → runtime set empty

    # the agent / operator adds a runtime job, then it's reapplied
    await session._reapply_cron({"cron": {"jobs": [_jd("runtime_job")]}})
    assert sched.get_job("runtime_job") is not None

    # the runtime job is removed from the file + reload → unscheduled
    await session._reapply_cron({"cron": {"jobs": []}})
    assert sched.get_job("runtime_job") is None       # removed (the S4 diff)
    assert sched.get_job("startup_job") is not None    # startup untouched


@pytest.mark.asyncio
async def test_boot_seeded_runtime_job_is_removable(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a runtime job present at boot (.reyn/cron.yaml) is tracked, so removing
    it from the file + reloading unschedules it (the boot-seed of the removal-diff)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    (tmp_path / ".reyn" / "config" / "cron.yaml").write_text(
        "cron:\n  jobs:\n    - name: boot_job\n      schedule: '* * * * *'\n"
        "      to: x\n      message: b\n",
        encoding="utf-8",
    )
    sched = CronScheduler([_job("boot_job")])  # the web boot loaded it into the scheduler
    set_active_scheduler(sched)
    session = _make_session(tmp_path)  # _runtime_cron_names seeded with boot_job

    await session._reapply_cron({"cron": {"jobs": []}})  # boot_job removed from the file
    assert sched.get_job("boot_job") is None


@pytest.mark.asyncio
async def test_cron_reapply_change_then_keep_does_not_remove(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a runtime job that is still present across reloads is NOT removed
    (the diff only unschedules jobs absent from the new file)."""
    monkeypatch.chdir(tmp_path)
    sched = CronScheduler([])
    set_active_scheduler(sched)
    session = _make_session(tmp_path)

    await session._reapply_cron({"cron": {"jobs": [_jd("keep_me")]}})
    await session._reapply_cron({"cron": {"jobs": [_jd("keep_me")]}})  # still present
    assert sched.get_job("keep_me") is not None  # not removed
