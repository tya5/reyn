from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.registry import AgentRegistry

logger = logging.getLogger(__name__)


@dataclass
class CronJob:
    """One scheduled execution (FP-0009 Component B + FP-0041 #489 PR-B).

    Two execution shapes co-exist:

      - **Message-based** (= FP-0041 PR-B, recommended): set ``to`` (=
        target agent name) and ``message`` (= free-form text). The
        scheduler dispatches the message to the agent's inbox with
        ``sender="cron:<name>"`` so the LLM reads it as a normal
        attributed turn from a scheduled trigger.

      - **Skill-based** (= legacy FP-0009): set ``skill`` (= skill name).
        The scheduler runs the skill directly via ``SkillRuntime.run``. Kept
        for backward compatibility with existing ``reyn.yaml``
        configurations.

    Exactly one shape should be set per job. If both ``skill`` AND
    ``to`` are set, the message-based path wins (= ``skill`` is
    ignored with a warning). If neither is set, the job is invalid.

    Mutable on the scheduler side: ``last_run_at`` / ``last_run_status``
    / ``last_run_error`` / ``next_run_at`` are updated after each fire.
    """

    name: str               # job identifier, unique within scheduler
    schedule: str           # cron expression, 5-field (e.g. "0 */6 * * *")
    # ── message-based (FP-0041 PR-B, recommended) ─────────────────
    to: str | None = None       # target agent name
    message: str | None = None  # free-form text dispatched to agent.inbox
    # FP-0043 S4b-3b: opt-in unattended notification channel (e.g. "telegram").
    # None = off (event-log only = current behaviour). When set, the fired cron
    # turn's final reply is routed to the channel via the external-transport outbox
    # interceptor (reply_to=ExternalRef), and a job-execution FAILURE is notified at
    # the runner level. The channel name maps to an MCP tool via reyn.yaml
    # external_transports (e.g. telegram→broker__post_message).
    notify: str | None = None
    # ── skill-based (FP-0009 legacy, backward compat) ──────────────
    skill: str | None = None    # skill name to run via SkillRuntime.run
    input: dict = field(default_factory=dict)
    # ── shared ─────────────────────────────────────────────────────
    enabled: bool = True
    last_run_at: datetime | None = None
    last_run_status: str | None = None   # "ok" | "error" | "cancelled" | None
    last_run_error: str | None = None    # short error description on failure
    next_run_at: datetime | None = None
    last_run_duration_seconds: float | None = None

    def is_message_based(self) -> bool:
        """True if this job uses the message-based shape (= ``to + message``)."""
        return bool(self.to and self.message)

    def to_dict(self) -> dict:
        """JSON-safe shape for `reyn cron list` and `reyn cron status`."""
        return {
            "name": self.name,
            "to": self.to,
            "message": self.message,
            "notify": self.notify,
            "skill": self.skill,
            "schedule": self.schedule,
            "input": self.input,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at is not None else None,
            "last_run_status": self.last_run_status,
            "last_run_error": self.last_run_error,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at is not None else None,
            "last_run_duration_seconds": self.last_run_duration_seconds,
        }


class CronScheduler:
    """Asyncio-based cron scheduler for stdlib + project skills.

    Each enabled job runs in its own asyncio.Task that sleeps until the
    next croniter-computed fire time, then dispatches the skill. Failures
    are recorded on the CronJob entry and logged at WARNING; the scheduler
    continues to the next interval (= no retry beyond the next fire).

    Lifecycle:
      - `start()` spawns one Task per enabled job.
      - `stop()` cancels all tasks and awaits them.
    Single scheduler instance per process; web mode attaches to
    `app.state.cron_scheduler`, CLI mode runs in the foreground.

    Time source:
      - `clock_fn` (= callable returning aware datetime) is injectable
        for tests. Production omits and uses `datetime.now(timezone.utc)`.

    Skill execution:
      - `runner_fn` (= async callable that runs the skill and returns
        a status string) is injectable. Production passes a function
        that resolves the skill via `load_dsl_skill` and runs through
        `SkillRuntime.run`.
      - If omitted, scheduler logs WARNING and marks status="error"
        with "no runner configured" so unconfigured deployments fail
        loudly rather than silently.
    """

    def __init__(
        self,
        jobs: list[CronJob],
        *,
        clock_fn: Callable[[], datetime] | None = None,
        runner_fn: Callable[[CronJob], "asyncio.Future"] | None = None,
    ) -> None:
        self._jobs: dict[str, CronJob] = {j.name: j for j in jobs}
        self._clock = clock_fn or (lambda: datetime.now(timezone.utc))
        self._runner = runner_fn  # may be None until set_runner is called
        self._tasks: dict[str, asyncio.Task] = {}
        self._running: bool = False

    @property
    def tasks(self) -> dict:
        """Read-only accessor for the running cron-task map (name → asyncio.Task)."""
        return self._tasks

    @property
    def running(self) -> bool:
        """Read-only flag: True between ``start()`` and ``stop()``."""
        return self._running

    def set_runner(self, runner_fn: Callable[[CronJob], "asyncio.Future"]) -> None:
        """Inject the runner after construction (= web lifespan needs the
        AgentRegistry which is created after the scheduler in some paths).
        """
        self._runner = runner_fn

    async def start(self) -> None:
        """Spawn one asyncio.Task per enabled job. Idempotent."""
        if self._running:
            return
        self._running = True
        for job in self._jobs.values():
            if job.enabled and job.name not in self._tasks:
                task = asyncio.create_task(
                    self._run_job_loop(job),
                    name=f"cron:{job.name}",
                )
                self._tasks[job.name] = task

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel all tasks; await up to ``timeout`` for cleanup."""
        self._running = False
        if not self._tasks:
            return
        for task in self._tasks.values():
            task.cancel()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        # Gather with return_exceptions so CancelledError doesn't propagate
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )

    def jobs(self) -> list[CronJob]:
        """Return all jobs (enabled + disabled), preserving insertion order."""
        return list(self._jobs.values())

    def get_job(self, name: str) -> CronJob | None:
        return self._jobs.get(name)

    # ── FP-0041 #489 PR-B2: live mutation API ──────────────────────────
    #
    # These methods support the LLM-callable ``cron`` action category
    # (= ``cron__register / unregister / enable / disable`` tools).
    # All mutations happen on the current event loop — the scheduler's
    # per-job tasks and the tool handlers share one asyncio loop in
    # both ``reyn web`` and ``reyn cron run``, so no lock is needed.

    async def add_job(self, job: CronJob) -> None:
        """Register ``job`` and (if running + enabled) spawn its task.

        Idempotency: if a job with the same name exists, it is replaced
        (= the existing task is cancelled first to prevent ghost dispatch
        from the stale schedule). Used by ``cron__register`` to swap
        job definitions without restart.
        """
        existing_task = self._tasks.pop(job.name, None)
        if existing_task is not None:
            existing_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(existing_task, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                pass
        self._jobs[job.name] = job
        if self._running and job.enabled:
            task = asyncio.create_task(
                self._run_job_loop(job),
                name=f"cron:{job.name}",
            )
            self._tasks[job.name] = task

    async def remove_job(self, name: str) -> bool:
        """Cancel + drop the job ``name``. Returns True iff the job
        existed and was removed."""
        if name not in self._jobs:
            return False
        task = self._tasks.pop(name, None)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                pass
        del self._jobs[name]
        return True

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle ``job.enabled``. When transitioning to enabled (and
        the scheduler is running), spawn the task; to disabled, cancel
        the running task. Returns True iff the job exists.

        Used by ``cron__enable`` / ``cron__disable`` tools to pause /
        resume jobs without removing them.
        """
        job = self._jobs.get(name)
        if job is None:
            return False
        job.enabled = bool(enabled)
        if not job.enabled:
            task = self._tasks.pop(name, None)
            if task is not None:
                task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(task, return_exceptions=True),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    pass
        elif self._running and name not in self._tasks:
            task = asyncio.create_task(
                self._run_job_loop(job),
                name=f"cron:{job.name}",
            )
            self._tasks[name] = task
        return True

    async def run_now(self, name: str) -> bool:
        """Trigger one-off execution outside the schedule. Returns True iff
        job exists and runner is configured. Updates last_run_* fields."""
        job = self._jobs.get(name)
        if job is None:
            return False
        if self._runner is None:
            return False
        await self._fire(job)
        return True

    def compute_next_run(self, job: CronJob, *, after: datetime | None = None) -> datetime | None:
        """Compute the next fire time for ``job.schedule`` after ``after``
        (or now). Returns None if the cron expression is invalid (= logs
        WARNING and disables the job in-place)."""
        from croniter import CroniterBadCronError, croniter

        start = after if after is not None else self._clock()
        try:
            it = croniter(job.schedule, start)
            return it.get_next(datetime)
        except (CroniterBadCronError, ValueError, KeyError) as exc:
            logger.warning(
                "CronJob %r has invalid schedule %r — disabling. Error: %s",
                job.name,
                job.schedule,
                exc,
            )
            job.enabled = False
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_job_loop(self, job: CronJob) -> None:
        """Per-job loop: sleep until next fire time, then run."""
        while self._running:
            next_at = self.compute_next_run(job)
            if next_at is None:
                return  # invalid cron; already logged + disabled
            job.next_run_at = next_at
            wait_seconds = max(0.0, (next_at - self._clock()).total_seconds())
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            await self._fire(job)

    async def _fire(self, job: CronJob) -> None:
        """Execute the job's skill via the runner, record outcome."""
        import time

        fired_at = self._clock()
        job.last_run_at = fired_at
        start = time.monotonic()

        if self._runner is None:
            logger.warning(
                "CronJob %r fired but no runner is configured — marking error.",
                job.name,
            )
            job.last_run_status = "error"
            job.last_run_error = "no runner configured"
            job.last_run_duration_seconds = time.monotonic() - start
            return

        try:
            result = await self._runner(job)
            job.last_run_status = result if isinstance(result, str) else "ok"
            job.last_run_error = None
        except asyncio.CancelledError:
            job.last_run_status = "cancelled"
            job.last_run_error = None
            job.last_run_duration_seconds = time.monotonic() - start
            raise
        except Exception as exc:
            short_err = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "CronJob %r raised an exception during execution: %s",
                job.name,
                short_err,
            )
            job.last_run_status = "error"
            job.last_run_error = short_err
        finally:
            job.last_run_duration_seconds = time.monotonic() - start


# ── FP-0041 #489 PR-B2: active scheduler registry ──────────────────────
#
# Module-level singleton so LLM-callable cron tools (= ``cron__register``
# / unregister / enable / disable) can reach the live scheduler in the
# same process. Set by whoever boots the scheduler (``reyn web``
# lifespan or ``reyn cron run`` foreground), queried by tool handlers.
#
# Returns None when no scheduler is registered (= CLI subcommand other
# than ``reyn cron run``, or process boot has not reached scheduler
# init yet). Tool handlers degrade gracefully: write to ``.reyn/cron.yaml``
# but skip live-update.
_active_scheduler: "CronScheduler | None" = None


def set_active_scheduler(scheduler: "CronScheduler | None") -> None:
    """Register / unregister the process-wide active scheduler."""
    global _active_scheduler
    _active_scheduler = scheduler


def get_active_scheduler() -> "CronScheduler | None":
    """Return the active scheduler, or None when unset."""
    return _active_scheduler
