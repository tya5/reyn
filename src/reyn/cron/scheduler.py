from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.chat.registry import AgentRegistry

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
        The scheduler runs the skill directly via ``Agent.run``. Kept
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
    # ── skill-based (FP-0009 legacy, backward compat) ──────────────
    skill: str | None = None    # skill name to run via Agent.run
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
        `Agent.run`.
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
