"""Webhook notification helper for FP-0001 push notifications.

The webhook is best-effort: failures are logged at WARNING and do
NOT propagate to the caller. A2A task progression must never block
on a non-responsive webhook peer.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# Default timeout — short enough that a slow webhook doesn't stall
# the skill's ask_user dispatch path.
_DEFAULT_TIMEOUT = 10.0


async def post_webhook(
    url: str,
    payload: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    _http_client: "httpx.AsyncClient | None" = None,
) -> None:
    """POST ``payload`` to ``url`` as JSON. Errors are logged, not raised.

    ``_http_client`` is an optional injectable ``httpx.AsyncClient`` used
    exclusively in tests (e.g. with ``httpx.MockTransport``). Production
    callers should omit it; a fresh client with the given ``timeout`` is
    created automatically.
    """
    try:
        import httpx
    except ImportError:
        logger.warning(
            "webhook post skipped: httpx not installed. "
            "Install with: pip install httpx",
        )
        return

    try:
        if _http_client is not None:
            response = await _http_client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "webhook post returned %d: url=%s",
                response.status_code,
                url,
            )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget
        logger.warning("webhook post failed: url=%s error=%s", url, exc)


__all__ = ["post_webhook"]
