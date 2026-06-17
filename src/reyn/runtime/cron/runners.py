"""Shared cron runner factory (FP-0009 + FP-0041 #489 PR-B).

Builds the ``runner_fn`` passed to ``CronScheduler``. Dispatches each
fired ``CronJob`` based on its shape:

  - **Message-based** (= FP-0041 PR-B, ``to + message``): pushes an
    envelope into the target agent's inbox with ``sender="cron:<name>"``
    attribution. The agent's router_loop consumes it as a normal
    attributed turn from a scheduled trigger.
  - **Skill-based** (= FP-0009 legacy, ``skill``): delegates to the
    legacy skill-running callable. Existing reyn.yaml configurations
    continue to work unchanged.

Two collaborators are injected (= keeps this factory transport-agnostic):

  - ``legacy_skill_runner(job) -> str``: the FP-0009 skill execution
    closure. Build with whatever Agent / config wiring the host
    process needs.
  - ``inbox_pusher(to, envelope) -> str``: deliver ``envelope`` to the
    target agent's inbox. In web mode this routes via the
    AgentRegistry. In CLI standalone mode no registry exists; pass
    ``None`` and message-based jobs will warn + return "error" instead
    of dispatching.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.cron.scheduler import CronJob

logger = logging.getLogger(__name__)


def build_default_runner(
    *,
    legacy_skill_runner: Callable[["CronJob"], Awaitable[str]] | None = None,
    inbox_pusher: Callable[[str, dict], Awaitable[str]] | None = None,
) -> Callable[["CronJob"], Awaitable[str]]:
    """Construct a CronScheduler-compatible runner.

    Parameters
    ----------
    legacy_skill_runner:
        ``async (job: CronJob) -> str`` that executes a skill-based job.
        When None, skill-based jobs return "error" with a warning.
    inbox_pusher:
        ``async (to: str, envelope: dict) -> str`` that delivers an
        envelope to the target agent's inbox. When None, message-based
        jobs return "error" with a warning (= e.g. CLI standalone mode
        with no AgentRegistry context).

    Returns
    -------
    Callable returning "ok" / "error" per fire. Exceptions propagate
    to the scheduler which records ``last_run_error``.
    """
    async def _runner(job: "CronJob") -> str:
        if job.is_message_based():
            if inbox_pusher is None:
                logger.warning(
                    "Cron job %r is message-based (to=%r) but no "
                    "inbox_pusher is configured — message-based "
                    "dispatch is not supported in this process "
                    "(= standalone `reyn cron run` lacks a session "
                    "registry; use `reyn web` with cron section).",
                    job.name, job.to,
                )
                return "error"
            envelope = {
                "text": job.message,
                "sender": f"cron:{job.name}",
            }
            return await inbox_pusher(job.to, envelope)
        # Skill-based legacy path.
        if legacy_skill_runner is None:
            logger.warning(
                "Cron job %r is skill-based (skill=%r) but no "
                "legacy_skill_runner is configured — skipping.",
                job.name, job.skill,
            )
            return "error"
        return await legacy_skill_runner(job)

    return _runner
