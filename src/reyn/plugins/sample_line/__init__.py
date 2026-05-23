"""sample_line — Sample LINE Messaging API webhook plugin (FP-0041 #489).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️

Mirror of ``sample_slack`` for LINE Messaging API, wired through
``line-bot-sdk`` v3 + ``reyn.plugins.api``. Reyn maintainers do NOT
commit to keeping this code working against LINE API drift. See
``README.md`` for the production guidance.

Plugin contract:
  - Entry point in ``pyproject.toml``:
    ``[project.entry-points."reyn.webhooks"]
       sample_line = "reyn.plugins.sample_line:register_router"``
  - ``register_router(config: dict) -> APIRouter | None``
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def register_router(config: dict) -> APIRouter | None:
    """Plugin entry point — return the LINE webhook router to mount.

    Skips (= returns None) when any of these are missing:
      - ``target_agent`` in config
      - ``LINE_CHANNEL_SECRET`` env var
      - ``line-bot-sdk`` package (= install ``reyn[sample_line]``)
    """
    target_agent = config.get("target_agent") if isinstance(config, dict) else None
    if not isinstance(target_agent, str) or not target_agent:
        logger.warning(
            "sample_line plugin: 'target_agent' missing in webhooks.yaml; skipping mount",
        )
        return None
    if not os.environ.get("LINE_CHANNEL_SECRET"):
        logger.warning(
            "sample_line plugin: LINE_CHANNEL_SECRET env var not set; skipping mount",
        )
        return None
    try:
        from .webhook import build_router
    except ImportError as exc:
        logger.warning(
            "sample_line plugin: required SDK not installed "
            "(install with ``pip install reyn[sample_line]``); skipping mount. "
            "Detail: %s",
            exc,
        )
        return None
    return build_router(target_agent=target_agent)
