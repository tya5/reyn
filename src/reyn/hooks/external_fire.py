"""reyn.hooks.external_fire — non-blocking dispatch helper for OUT-OF-SESSION
external-event ingress (#2608 H5: cron + webhook).

H1 (``mcp_resource_updated``) and H4 (``file_changed``) both fire their hook
from INSIDE the session's own process — a bounded queue drained on a
dedicated task/loop, decoupling the producer (MCP receive-loop task / watchdog
thread) from the hook dispatch. Cron and webhook ingress have no such
producer/drain split: ``reyn.runtime.cron.routing.resolve_cron_session`` /
``reyn.runtime.webhook_routing.resolve_webhook_session`` resolve the target
Session directly at fire/request time, in the SAME coroutine that also does
the ingress's own delivery work (the cron job's inbox push / the webhook
plugin's HTTP response).

:func:`fire_and_forget` is the H5 non-blocking discipline: the hook dispatch
is scheduled as a background ``asyncio.create_task`` rather than awaited
inline, so a slow hook action (a ``shell_exec`` that runs a multi-second
command) can never stall the cron job's own delivery or delay the webhook
plugin's response. ``HookDispatcher.dispatch`` already isolates per-hook
failures internally (never raises — see ``reyn.hooks.dispatcher``); the
second ``try/except`` here exists purely so an unretrieved background-task
exception never surfaces an "exception was never retrieved" asyncio warning.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def fire_and_forget(session: Any, point: str, template_vars: dict) -> None:
    """Schedule ``session.dispatch_external_event(point, template_vars)`` as a
    background task rather than awaiting it inline.

    Never raises into the caller — safe to call unconditionally from an
    ingress's fast path, empty-hook-registry included (``dispatch`` itself is
    a no-op when nothing is registered for ``point``, so an empty registry is
    byte-identical to a build with no hook mechanism at all beyond the
    negligible cost of scheduling one no-op task).
    """

    async def _run() -> None:
        try:
            await session.dispatch_external_event(point, template_vars)
        except Exception:  # noqa: BLE001 — background dispatch must never raise into the caller
            logger.warning(
                "external-event hook dispatch failed for point=%r", point, exc_info=True,
            )

    asyncio.create_task(_run())


__all__ = ["fire_and_forget"]
