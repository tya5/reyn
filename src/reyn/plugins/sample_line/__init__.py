"""sample_line — Sample LINE Messaging API webhook plugin (FP-0041 #489 PR-E).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️

Mirror of ``sample_slack`` for LINE Messaging API. Reyn maintainers
do NOT commit to keeping this code working against LINE API drift.
See ``README.md`` for the production guidance.

Plugin contract:
  - Entry point in ``pyproject.toml``:
    ``[project.entry-points."reyn.webhooks"] sample_line = "reyn.plugins.sample_line:register_router"``
  - ``register_router(config: dict) -> APIRouter | None``
    receives the plugin's section of ``webhooks.yaml``, returns the
    router to mount or None to skip.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def register_router(config: dict) -> APIRouter | None:
    """Plugin entry point — return the LINE webhook router to mount.

    Reads required config:
      - ``target_agent``: name of the agent that receives incoming
        LINE messages on its inbox.
      - ``LINE_CHANNEL_SECRET`` env var: LINE's channel secret
        (= from the LINE Developers Console).

    Returns ``None`` (= skip) when either is missing. Reyn loader
    logs a warning, route is not mounted.
    """
    target_agent = config.get("target_agent") if isinstance(config, dict) else None
    if not isinstance(target_agent, str) or not target_agent:
        logger.warning(
            "sample_line plugin: 'target_agent' missing in config; skipping mount",
        )
        return None
    if not os.environ.get("LINE_CHANNEL_SECRET"):
        logger.warning(
            "sample_line plugin: LINE_CHANNEL_SECRET env var not set; skipping mount",
        )
        return None
    from .webhook import build_router
    return build_router(target_agent=target_agent)
