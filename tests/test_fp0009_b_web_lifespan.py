"""Tier 2 OS-invariant tests for FP-0009 Component B — web server lifespan.

Verifies that ``_lifespan`` correctly starts / stops the cron scheduler on
web gateway boot and that the FP-0001 RunRegistry is still initialised by
the same lifespan (= no regression from moving it out of module scope).

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- Tier 2 OS invariant: exercises the lifespan state machine directly.
- No unittest.mock / MagicMock / AsyncMock / patch usage.
- Real CronScheduler / CronJob instances are created by the lifespan; tests
  observe them via app.state (public surface).
- load_config() is driven by a real reyn.yaml written to ``tmp_path`` — no
  monkey-patching of internal Config objects.  ``monkeypatch.chdir`` is used
  to point load_config's CWD-based project-root search at the tmp directory.
- Scheduler runner is replaced via the public ``set_runner()`` method BEFORE
  any job fires; a far-future cron schedule ("59 23 31 12 5") means no job
  fires during the test regardless.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Skip entire module if fastapi / httpx are not installed (same guard as
# the other web tests).
pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_reyn_yaml(directory: Path, content: str) -> None:
    """Write a reyn.yaml file in ``directory``."""
    (directory / "reyn.yaml").write_text(content, encoding="utf-8")


async def _run_lifespan(app):
    """Execute the lifespan startup, yield app.state snapshot, then shutdown.

    Returns app.state after startup so tests can inspect scheduler / registry.
    """
    # Import here so module-level guards have already run.
    from reyn.interfaces.web.server import _lifespan

    async with _lifespan(app):
        # Capture the state inside the lifespan (= after startup, before shutdown)
        return app.state


def _make_bare_app():
    """Return a minimal FastAPI app with no routers (avoids import side effects)."""
    from fastapi import FastAPI
    return FastAPI()


# ---------------------------------------------------------------------------
# Test 1: no cron: block → scheduler is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cron_block_scheduler_is_none(tmp_path, monkeypatch):
    """Tier 2: lifespan sets cron_scheduler=None when reyn.yaml has no cron: block."""
    _write_reyn_yaml(tmp_path, "model: standard\n")
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()
    state = await _run_lifespan(app)

    assert state.cron_scheduler is None


# ---------------------------------------------------------------------------
# Test 2: empty cron.jobs: [] → scheduler is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_cron_jobs_scheduler_is_none(tmp_path, monkeypatch):
    """Tier 2: lifespan sets cron_scheduler=None when cron.jobs is empty."""
    _write_reyn_yaml(
        tmp_path,
        "model: standard\ncron:\n  jobs: []\n",
    )
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()
    state = await _run_lifespan(app)

    assert state.cron_scheduler is None


# ---------------------------------------------------------------------------
# Test 3: all jobs disabled → scheduler is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_jobs_disabled_scheduler_is_none(tmp_path, monkeypatch):
    """Tier 2: lifespan sets cron_scheduler=None when every cron job is disabled."""
    _write_reyn_yaml(
        tmp_path,
        (
            "model: standard\n"
            "cron:\n"
            "  jobs:\n"
            "    - name: nightly_disabled\n"
            "      skill: some_skill\n"
            "      schedule: '59 23 31 12 5'\n"
            "      enabled: false\n"
        ),
    )
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()
    state = await _run_lifespan(app)

    assert state.cron_scheduler is None


# ---------------------------------------------------------------------------
# Test 4: at least one enabled job → scheduler is a CronScheduler instance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enabled_job_scheduler_is_cron_scheduler_instance(tmp_path, monkeypatch):
    """Tier 2: lifespan sets cron_scheduler to a CronScheduler when ≥1 enabled job exists."""
    from reyn.runtime.cron import CronScheduler

    _write_reyn_yaml(
        tmp_path,
        (
            "model: standard\n"
            "cron:\n"
            "  jobs:\n"
            "    - name: far_future_job\n"
            "      skill: some_skill\n"
            "      schedule: '59 23 31 12 5'\n"
            "      enabled: true\n"
        ),
    )
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()

    # Use a no-op runner to prevent any actual skill resolution during the test.
    # We inject it via set_runner() before any job can fire (the schedule is
    # far-future so no fire occurs within the test, but defensive anyway).
    async def _noop_runner(job):
        return "ok"

    from reyn.interfaces.web.server import _lifespan

    async with _lifespan(app):
        # Verify scheduler is a CronScheduler
        assert isinstance(app.state.cron_scheduler, CronScheduler)

        # Replace the production runner with the no-op so the test remains
        # self-contained even if scheduling unexpectedly fires.
        app.state.cron_scheduler.set_runner(_noop_runner)

        # Verify the job is registered
        jobs = app.state.cron_scheduler.jobs()
        (job,) = jobs  # exactly one job was configured
        assert job.name == "far_future_job"
        assert job.skill == "some_skill"
        assert job.enabled is True

    # After the context exits, stop() has been called
    assert app.state.cron_scheduler is not None  # reference persists on state


# ---------------------------------------------------------------------------
# Test 5: shutdown stops the scheduler (_running becomes False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_stops_scheduler(tmp_path, monkeypatch):
    """Tier 2: lifespan shutdown calls stop() on the scheduler, clearing tasks."""
    _write_reyn_yaml(
        tmp_path,
        (
            "model: standard\n"
            "cron:\n"
            "  jobs:\n"
            "    - name: stop_test_job\n"
            "      skill: some_skill\n"
            "      schedule: '59 23 31 12 5'\n"
            "      enabled: true\n"
        ),
    )
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()

    stopped_event = asyncio.Event()
    original_stop = None

    from reyn.interfaces.web.server import _lifespan

    async with _lifespan(app):
        scheduler = app.state.cron_scheduler
        assert scheduler is not None

        # Wrap stop() to record that it was called.
        _original_stop = scheduler.stop

        async def _recording_stop(**kwargs):
            stopped_event.set()
            return await _original_stop(**kwargs)

        scheduler.stop = _recording_stop  # type: ignore[method-assign]

    # The lifespan __aexit__ has now run; stop() should have been called.
    assert stopped_event.is_set(), "scheduler.stop() was not called during shutdown"


# ---------------------------------------------------------------------------
# Test 6: run_registry is set (FP-0001 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_registry_is_set_in_lifespan(tmp_path, monkeypatch):
    """Tier 2: lifespan sets app.state.run_registry (FP-0001 not regressed by move)."""
    from reyn.interfaces.web.run_registry import RunRegistry

    _write_reyn_yaml(tmp_path, "model: standard\n")
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()

    from reyn.interfaces.web.server import _lifespan

    async with _lifespan(app):
        assert isinstance(app.state.run_registry, RunRegistry)

    # registry reference persists after shutdown (no cleanup in stop path)
    assert isinstance(app.state.run_registry, RunRegistry)


# ---------------------------------------------------------------------------
# Test 7: boot-time error in cron config is swallowed (defensive boot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_cron_schedule_does_not_prevent_boot(tmp_path, monkeypatch):
    """Tier 2: a bad schedule expression is caught defensively and cron_scheduler stays None."""
    # Note: _build_cron_config validates fields and raises ValueError for
    # missing/empty name/skill/schedule. An empty string triggers the guard.
    # We use an entry that passes yaml parsing but would cause the scheduler
    # to log a warning and disable the job (invalid cron expression after boot).
    # The safest trigger here is to write a yaml that raises during CronJob
    # construction — but since CronJob is a dataclass it won't raise.
    # Instead: rely on the fact that a completely empty schedule str is
    # rejected by _build_cron_config (raises ValueError), which is caught by
    # the lifespan's bare except.  We need to provoke the exception from
    # _build_cron_config → so we write a job with an integer schedule.
    # YAML parses `schedule: 123` as an int; _build_cron_config rejects it.
    _write_reyn_yaml(
        tmp_path,
        (
            "model: standard\n"
            "cron:\n"
            "  jobs:\n"
            "    - name: bad_job\n"
            "      skill: some_skill\n"
            "      schedule: 123\n"  # integer, not string — triggers ValueError
        ),
    )
    monkeypatch.chdir(tmp_path)

    app = _make_bare_app()
    state = await _run_lifespan(app)

    # Boot must succeed and scheduler stays None
    assert state.cron_scheduler is None
    # RunRegistry must still be set (= startup continued after cron failure)
    from reyn.interfaces.web.run_registry import RunRegistry
    assert isinstance(state.run_registry, RunRegistry)
