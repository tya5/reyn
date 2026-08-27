"""Shared cron runner factory (FP-0009 + FP-0041 #489 PR-B; ``action`` #5209).

Builds the ``runner_fn`` passed to ``CronScheduler``. Each fired
``CronJob`` declares its ``action`` (#5209):

  - ``"message"`` (default, FP-0041 PR-B, ``to + message``): the runner
    pushes an envelope into the target agent's inbox with
    ``sender="cron:<name>"`` attribution, and the agent's router_loop
    consumes it as a normal attributed turn from a scheduled trigger —
    always starts an LLM turn.
  - ``"hook"`` (#5209): the runner only fires the ``cron_fired``
    external-event hook on the job's host session — no inbox push, no
    turn started by this runner itself. Whatever happens next is up to a
    ``hooks.yaml`` ``on: cron_fired`` entry's own ``push_when`` (a
    condition-gated ``exec_capture``), which can cost zero LLM turns when
    unsatisfied — the reason #5209 exists ("token 0 の定期検査").

(``to`` is required for every job regardless of ``action`` — it is the
host agent whose session ``cron_fired`` fires on. A config entry missing
the shape its ``action`` requires is rejected at load.)

The transport collaborators are injected (= keeps this factory transport-agnostic):

  - ``inbox_pusher(to, envelope, native_id) -> str``: deliver ``envelope``
    to the target agent's inbox (``action="message"`` jobs only). In web
    mode this routes via the AgentRegistry. In CLI standalone mode no
    registry exists; pass ``None`` and message-based jobs will warn +
    return "error" instead of dispatching.
  - ``hook_only_dispatcher(to, native_id) -> str`` (#5209): resolve the
    host session and fire ``cron_fired`` on it, WITHOUT pushing anything
    to the inbox (``action="hook"`` jobs only). ``None`` behaves the same
    as a missing ``inbox_pusher`` — warn + "error".
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
    hook_only_dispatcher: Callable[[str, str], Awaitable[str]] | None = None,
    failure_notifier: Callable[["CronJob", str], Awaitable[None]] | None = None,
) -> Callable[["CronJob"], Awaitable[str]]:
    """Construct a CronScheduler-compatible runner.

    Parameters
    ----------
    inbox_pusher:
        ``async (to: str, envelope: dict, native_id: str) -> str`` that
        delivers an envelope to the target agent's inbox. ``native_id`` is the
        job name (FP-0043 S4b-3a routing-key native-id) so the pusher routes to
        the job's own ``cron:<job_name>`` Session. Used for ``action="message"``
        jobs only. When None, message-based jobs return "error" with a
        warning (= e.g. CLI standalone mode with no AgentRegistry context).
    hook_only_dispatcher:
        ``async (to: str, native_id: str) -> str`` (#5209) that resolves the
        job's host session and fires ``cron_fired`` on it — no inbox push.
        Used for ``action="hook"`` jobs only. When None, hook jobs return
        "error" with a warning, same shape as a missing ``inbox_pusher``.
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
        if job.is_hook_based():
            # #5209: fire cron_fired only — never push to the inbox, never
            # start a turn itself. That's entirely a hooks.yaml on:cron_fired
            # entry's own decision (push_when).
            if not job.to:
                logger.warning(
                    "Cron job %r is action=hook but has no 'to' (host "
                    "agent) — skipping. Config load should have rejected "
                    "this; treating as malformed.",
                    job.name,
                )
                return "error"
            if hook_only_dispatcher is None:
                logger.warning(
                    "Cron job %r is action=hook (to=%r) but no "
                    "hook_only_dispatcher is configured — hook-only "
                    "dispatch is not supported in this process "
                    "(= standalone `reyn cron run` lacks a session "
                    "registry; use `reyn web` with cron section).",
                    job.name, job.to,
                )
                return "error"
            try:
                result = await hook_only_dispatcher(job.to, job.name)
            except Exception as exc:
                await _notify_failure(job, f"{type(exc).__name__}: {exc}")
                raise
            if result == "error":
                await _notify_failure(job, "dispatch failed (could not resolve cron session)")
            return result
        if not job.is_message_based():
            # A non-hook, non-message job here is malformed — config
            # rejects any entry lacking its action's required shape at load.
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
