"""sample_slack — Sample Slack webhook plugin (FP-0041 #489 follow-up).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️

Demonstrates Reyn's plugin framework for chat-transport webhook
integrations. Reyn maintainers do NOT commit to keeping this code
working against Slack API drift. See ``README.md`` for the production
guidance.

Plugin contract:
  - Entry point in ``pyproject.toml``:
    ``[project.entry-points."reyn.webhooks"] sample_slack = "reyn.plugins.sample_slack:register_router"``
  - ``register_router(config: dict) -> APIRouter | None``
    receives this plugin's section of ``reyn.yaml`` (= the dict keyed
    by the plugin name at top-level), returns a router to mount or
    None to skip.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def register_router(config: dict) -> APIRouter | None:
    """Plugin entry point — return the Slack webhook router to mount.

    Reads required config:
      - ``target_agent``: name of the agent that receives incoming
        Slack messages on its inbox.
      - ``SLACK_SIGNING_SECRET`` env var: Slack's HMAC signing secret
        (= configured at api.slack.com per the README).

    Returns None (= skip) when either is missing — Reyn loader logs a
    warning, the route is not mounted. Operator sees the missing
    config in logs without crashing reyn web.
    """
    target_agent = config.get("target_agent") if isinstance(config, dict) else None
    if not isinstance(target_agent, str) or not target_agent:
        logger.warning(
            "sample_slack plugin: 'target_agent' missing in config; skipping mount",
        )
        return None
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        logger.warning(
            "sample_slack plugin: SLACK_SIGNING_SECRET env var not set; skipping mount",
        )
        return None
    from .webhook import build_router
    return build_router(target_agent=target_agent)
