"""Shared cron runner factory (FP-0009 + FP-0041 #489 PR-B).

Builds the ``runner_fn`` passed to ``CronScheduler``. Each fired
``CronJob`` is message-based (= FP-0041 PR-B, ``to + message``): the
runner pushes an envelope into the target agent's inbox with
``sender="cron:<name>"`` attribution, and the agent's router_loop
consumes it as a normal attributed turn from a scheduled trigger.

(A cron job must be message-based: ``to`` + ``message``. A config entry
lacking that shape — e.g. a legacy bare ``skill`` name — is rejected at load.)

The transport collaborator is injected (= keeps this factory transport-agnostic):

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
    inbox_pusher: Callable[[str, dict, str], Awaitable[str]] | None = None,
    failure_notifier: Callable[["CronJob", str], Awaitable[None]] | None = None,
) -> Callable[["CronJob"], Awaitable[str]]:
    """Construct a CronScheduler-compatible runner.

    Parameters
    ----------
    inbox_pusher:
        ``async (to: str, envelope: dict, native_id: str) -> str`` that
        delivers an envelope to the target agent's inbox. ``native_id`` is the
        job name (FP-0043 S4b-3a routing-key native-id) so the pusher routes to
        the job's own ``cron:<job_name>`` Session. When None, message-based jobs
        return "error" with a warning (= e.g. CLI standalone mode with no
        AgentRegistry context).
    failure_notifier:
        ``async (job: CronJob, reason: str) -> None`` invoked when a job with an
        opt-in ``notify`` channel FAILS to dispatch (FP-0043 S4b-3b, errors = (b)
        runner-level). The successful turn's final reply is relayed via the outbox
        interceptor (not here); this covers execution failures that never produce a
        reply. None / no ``job.notify`` → no failure notification. Best-effort: the
        notifier's own exceptions are swallowed so notify never fails the job.

    Returns
    -------
    Callable returning "ok" / "error" per fire. Exceptions propagate
    to the scheduler which records ``last_run_error``.
    """
    async def _notify_failure(job: "CronJob", reason: str) -> None:
        if not job.notify or failure_notifier is None:
            return
        try:
            await failure_notifier(job, reason)
        except Exception:  # noqa: BLE001 — notify is best-effort, never fail the job
            logger.warning(
                "cron failure-notify raised for job %r (channel=%r)",
                job.name, job.notify,
            )

    async def _runner(job: "CronJob") -> str:
        if not job.is_message_based():
            # All cron jobs are message-based; config rejects any legacy
            # non-message entry at load. A non-message job here is malformed.
            logger.warning(
                "Cron job %r is not message-based (to=%r, message set=%s) — "
                "skipping. Set both 'to' and 'message'.",
                job.name, job.to, bool(job.message),
            )
            return "error"
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
        # FP-0043 S4b-3b: carry the opt-in notify channel so the pusher sets
        # reply_to=ExternalRef → the final reply routes to the channel via the
        # outbox interceptor.
        if job.notify:
            envelope["notify"] = job.notify
        # FP-0043 S4b-3a: pass job.name as the routing-key native-id so the
        # pusher delivers to the job's own cron:<job_name> Session.
        try:
            result = await inbox_pusher(job.to, envelope, job.name)
        except Exception as exc:
            await _notify_failure(job, f"{type(exc).__name__}: {exc}")
            raise
        if result == "error":
            await _notify_failure(job, "dispatch failed (could not deliver to cron session)")
        return result

    return _runner
