"""Webhook notification helper for FP-0001 push notifications.

Pre-#269 the webhook was fire-and-forget: HTTP 2xx / 4xx / 5xx all
logged but never returned to the caller. issue #269 upgrades the
contract: ``post_webhook`` now returns a ``DeliveryResult`` so callers
can track per-channel liveness (= ``ChannelState`` in
``reyn.runtime.channel_state``) + drive stall detection for #268's
origin-pinned intervention routing.

Backwards-compat: callers that ignore the return value see no change
in behaviour — the delivery still happens, errors still log at
WARNING, A2A task progression never blocks on a non-responsive
webhook peer. The new shape is purely additive.

issue #269 — A2A spec range で組む (= HTTP 2xx + retry policy is
standard webhook convention、 custom protocol なし).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from reyn.runtime.channel_state import (
    DEFAULT_RETRY_POLICY,
    DeliveryOutcome,
    DeliveryResult,
    RetryPolicy,
)

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
    retry_policy: RetryPolicy | None = None,
    _http_client: "httpx.AsyncClient | None" = None,
) -> DeliveryResult:
    """POST ``payload`` to ``url`` as JSON. Returns ``DeliveryResult``.

    issue #269: the return value now lets callers update per-channel
    state machines (= ``ChannelState.record_attempt(result)``) instead
    of inferring success from log noise. ``response.status_code >= 400``
    + transport errors are categorised so the caller can decide whether
    to retry, mark the channel dead, or escalate.

    Retry policy: when ``retry_policy.max_attempts > 1``, transient
    failures (= 5xx / timeout / transport error) trigger automatic
    retries with the configured backoff. Permanent failures (= 4xx)
    short-circuit without retry. ``None`` uses ``DEFAULT_RETRY_POLICY``
    (= 3 attempts, 0.5s + 2s backoff). Set
    ``retry_policy=NO_RETRY_POLICY`` for pre-#269 fire-and-forget
    semantics (= one attempt only).

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
        return DeliveryResult(outcome=DeliveryOutcome.NO_TRANSPORT)

    policy = retry_policy or DEFAULT_RETRY_POLICY
    last_result: DeliveryResult | None = None

    for attempt_idx in range(policy.max_attempts):
        last_result = await _post_once(
            url, payload, timeout=timeout, httpx_mod=httpx,
            http_client=_http_client,
        )
        if last_result.ok:
            return last_result
        if last_result.outcome is DeliveryOutcome.PERMANENT_FAILURE:
            # 4xx — peer rejected, retry won't help.
            return last_result
        # Retryable: 5xx / timeout / transport error.
        # Wait for the configured backoff before the next attempt
        # (= last attempt has no follow-up sleep).
        if attempt_idx < policy.max_attempts - 1:
            backoff = policy.backoff_seconds[attempt_idx]
            await asyncio.sleep(backoff)

    # Exhausted retries; return the last failure.
    assert last_result is not None  # noqa: S101
    return last_result


async def _post_once(
    url: str,
    payload: dict,
    *,
    timeout: float,
    httpx_mod: object,
    http_client: "httpx.AsyncClient | None",
) -> DeliveryResult:
    """One attempt at POSTing the payload, returning categorised result."""
    try:
        if http_client is not None:
            response = await http_client.post(url, json=payload)
        else:
            async with httpx_mod.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001 — transport errors categorised
        logger.warning("webhook post failed: url=%s error=%s", url, exc)
        return DeliveryResult(
            outcome=DeliveryOutcome.RETRYABLE_FAILURE,
            status_code=None,
            error=str(exc),
        )

    status = response.status_code
    if 200 <= status < 300:
        return DeliveryResult(
            outcome=DeliveryOutcome.SUCCESS,
            status_code=status,
        )
    if 400 <= status < 500:
        logger.warning(
            "webhook post returned 4xx (permanent failure): url=%s status=%d",
            url, status,
        )
        return DeliveryResult(
            outcome=DeliveryOutcome.PERMANENT_FAILURE,
            status_code=status,
        )
    # 5xx or other non-2xx — treat as retryable.
    logger.warning(
        "webhook post returned %d (retryable): url=%s",
        status, url,
    )
    return DeliveryResult(
        outcome=DeliveryOutcome.RETRYABLE_FAILURE,
        status_code=status,
    )


__all__ = ["post_webhook"]
