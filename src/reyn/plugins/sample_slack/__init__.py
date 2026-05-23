"""sample_slack — Sample Slack webhook plugin (FP-0041 #489).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️

Demonstrates Reyn's plugin framework for chat-transport webhook
integrations via ``slack-bolt`` (= Slack's official SDK) + the
``reyn.plugins.api`` stable contract. Reyn maintainers do NOT
commit to keeping this code working against Slack API drift. See
``README.md`` for the production guidance.

Plugin contract:
  - Entry point in ``pyproject.toml``:
    ``[project.entry-points."reyn.webhooks"]
       sample_slack = "reyn.plugins.sample_slack:register_router"``
  - ``register_router(config: dict) -> APIRouter | None``
    receives the per-instance dict from ``webhooks.yaml``, returns
    the router to mount or None to skip (= missing dep / config).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def register_router(config: dict) -> APIRouter | None:
    """Plugin entry point — return the Slack webhook router to mount.

    Skips (= returns None) when any of these are missing:
      - ``target_agent`` in config (= operator forgot to declare)
      - ``SLACK_SIGNING_SECRET`` env var (= operator forgot to set)
      - ``slack-bolt`` package (= operator didn't install
        ``reyn[sample_slack]``)
    """
    target_agent = config.get("target_agent") if isinstance(config, dict) else None
    if not isinstance(target_agent, str) or not target_agent:
        logger.warning(
            "sample_slack plugin: 'target_agent' missing in webhooks.yaml; skipping mount",
        )
        return None
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        logger.warning(
            "sample_slack plugin: SLACK_SIGNING_SECRET env var not set; skipping mount",
        )
        return None
    try:
        from .webhook import build_router
    except ImportError as exc:
        logger.warning(
            "sample_slack plugin: required SDK not installed "
            "(install with ``pip install reyn[sample_slack]``); skipping mount. "
            "Detail: %s",
            exc,
        )
        return None
    return build_router(target_agent=target_agent)
