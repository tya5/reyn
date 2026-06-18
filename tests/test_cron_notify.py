"""Tier 2: FP-0043 S4b-3b — cron notify output layer (runner hook + config).

The notify layer stands on the existing FP-0041 external-transport outbox
interceptor: a notify-configured cron job tags its inbox with the channel (→
``reply_to=ExternalRef`` → the interceptor relays the agent's final reply), and a
job-execution FAILURE (errors = (b), runner-level) calls an injected failure
notifier. opt-in default off — no ``notify`` reproduces today's event-log-only
behaviour. These pin the runner hook + config parse chainlit/web-free (the
mcp_dispatcher is injected, so no real broker/telegram is needed).

Falsification (feedback_falsify_acceptance_test_before_proof): the opt-in tests red
if notify stops gating; the failure tests red if the hook stops firing (companion
comments).
"""
from __future__ import annotations

import pytest

from reyn.runtime.cron import CronJob
from reyn.runtime.cron.runners import build_default_runner


def _msg_job(*, notify=None) -> CronJob:
    return CronJob(
        name="news", schedule="0 9 * * *", to="agent_x", message="hi", notify=notify,
    )


@pytest.mark.asyncio
async def test_notify_channel_flows_to_the_pusher_envelope():
    """Tier 2: a notify-configured job carries the channel to the pusher (→ reply_to)."""
    seen: list = []

    async def _pusher(to, envelope, native_id):
        seen.append(envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_pusher)
    await runner(_msg_job(notify="telegram"))
    assert seen[0].get("notify") == "telegram"


@pytest.mark.asyncio
async def test_no_notify_means_no_channel_tag():
    """Tier 2: opt-in OFF — no notify → no channel tag (event-log only = today)."""
    seen: list = []

    async def _pusher(to, envelope, native_id):
        seen.append(envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_pusher)
    await runner(_msg_job(notify=None))
    assert "notify" not in seen[0]


@pytest.mark.asyncio
async def test_failure_notifier_fires_on_dispatch_error():
    """Tier 2: errors=(b) — a notify job that fails to dispatch calls the notifier."""
    notified: list = []

    async def _pusher(to, envelope, native_id):
        return "error"          # delivery failed

    async def _notifier(job, reason):
        notified.append((job.name, reason))

    runner = build_default_runner(inbox_pusher=_pusher, failure_notifier=_notifier)
    result = await runner(_msg_job(notify="telegram"))
    assert result == "error"
    assert notified and notified[0][0] == "news"


@pytest.mark.asyncio
async def test_failure_notifier_silent_without_notify():
    """Tier 2: a failing job WITHOUT notify does NOT call the notifier (opt-in)."""
    notified: list = []

    async def _pusher(to, envelope, native_id):
        return "error"

    async def _notifier(job, reason):
        notified.append(job.name)

    runner = build_default_runner(inbox_pusher=_pusher, failure_notifier=_notifier)
    await runner(_msg_job(notify=None))
    assert notified == []        # opt-in off → no failure notification


@pytest.mark.asyncio
async def test_failure_notifier_fires_on_exception_then_reraises():
    """Tier 2: an exception during dispatch notifies, then re-raises for the scheduler."""
    notified: list = []

    async def _pusher(to, envelope, native_id):
        raise RuntimeError("boom")

    async def _notifier(job, reason):
        notified.append(reason)

    runner = build_default_runner(inbox_pusher=_pusher, failure_notifier=_notifier)
    with pytest.raises(RuntimeError):       # propagates so the scheduler records it
        await runner(_msg_job(notify="telegram"))
    assert notified and "boom" in notified[0]


def test_notify_field_parses_from_config_message_shape():
    """Tier 2: cron.jobs[].notify parses onto CronJobConfig (message-based)."""
    from reyn.config.infra import _build_cron_config

    cfg = _build_cron_config({
        "jobs": [{
            "name": "morning_news", "to": "news_agent",
            "message": "今日のニュース", "schedule": "0 9 * * *",
            "notify": "telegram",
        }],
    })
    assert cfg.jobs[0].notify == "telegram"


def test_notify_ignored_for_skill_shape():
    """Tier 2: a skill-based job has no conversational reply → notify is dropped."""
    from reyn.config.infra import _build_cron_config

    cfg = _build_cron_config({
        "jobs": [{
            "name": "index", "skill": "index_events",
            "schedule": "0 9 * * *", "notify": "telegram",
        }],
    })
    assert cfg.jobs[0].notify is None
