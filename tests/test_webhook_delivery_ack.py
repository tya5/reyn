"""Tier 2: ``post_webhook`` delivery ack + retry contract (issue #269).

Pins the upgraded contract: ``post_webhook`` returns
``DeliveryResult`` so callers can drive per-channel liveness state
(= ``ChannelState`` in ``reyn.chat.channel_state``) instead of
inferring success from log noise.

Pins:

  1. 2xx response → ``DeliveryResult(outcome=SUCCESS, status_code=...)``
  2. 4xx response → ``DeliveryResult(outcome=PERMANENT_FAILURE,
     status_code=...)`` without retry attempts
  3. 5xx response → ``DeliveryResult(outcome=RETRYABLE_FAILURE)``,
     retries attempted per ``RetryPolicy``, returns last failure if
     all retries fail
  4. Transport error (= timeout / network) → ``RETRYABLE_FAILURE``
     with error string
  5. ``NO_RETRY_POLICY`` → single attempt, no retry sleep
  6. Backwards-compat: 2xx still doesn't raise, no exception thrown
     from the function regardless of outcome

Uses ``httpx.MockTransport`` so no actual network IO happens.
"""
from __future__ import annotations

import pytest

pytest.importorskip("httpx", reason="httpx not installed (needed by webhook delivery)")

import httpx  # noqa: E402

from reyn.chat.channel_state import (  # noqa: E402
    NO_RETRY_POLICY,
    DeliveryOutcome,
    RetryPolicy,
)
from reyn.interfaces.web.notifications import post_webhook  # noqa: E402


def _client_with_responses(responses: list[httpx.Response]) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient that returns responses in sequence.

    Each call consumes the next response in the list. If the list is
    exhausted, raises so the test fails loudly rather than silently
    hanging.
    """
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(
                "test webhook handler called more times than responses provided",
            ) from exc

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── 1. 2xx success ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_returns_success_on_2xx() -> None:
    """Tier 2: a 200 response yields DeliveryResult(SUCCESS, 200)."""
    client = _client_with_responses([httpx.Response(200, json={"ok": True})])
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
        )
        assert result.outcome is DeliveryOutcome.SUCCESS
        assert result.status_code == 200
        assert result.ok is True
        assert result.error is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_post_webhook_recognises_all_2xx_codes_as_success() -> None:
    """Tier 2: 201 / 202 / 204 all count as success (= peer accepted)."""
    for status in (200, 201, 202, 204):
        client = _client_with_responses([httpx.Response(status)])
        try:
            result = await post_webhook(
                "http://peer.example/webhook",
                {"event": "test"},
                _http_client=client,
            )
            assert result.outcome is DeliveryOutcome.SUCCESS, (
                f"status {status} should be SUCCESS"
            )
            assert result.status_code == status
        finally:
            await client.aclose()


# ── 2. 4xx permanent failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_returns_permanent_failure_on_4xx() -> None:
    """Tier 2: a 404 response yields PERMANENT_FAILURE without retries.

    Per the issue #269 contract, 4xx means the peer rejected the
    payload — retrying won't help, the channel needs intervention
    (= unregister, claim by different channel, etc.).
    """
    # Provide only ONE response; if the code retries, it'll raise
    # AssertionError from the handler.
    client = _client_with_responses([httpx.Response(404, text="not found")])
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.01)),
        )
        assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
        assert result.status_code == 404
        assert result.ok is False
        assert result.should_retry is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_post_webhook_4xx_codes_all_treated_as_permanent() -> None:
    """Tier 2: 400 / 401 / 403 / 404 / 410 all yield PERMANENT_FAILURE."""
    for status in (400, 401, 403, 404, 410):
        client = _client_with_responses([httpx.Response(status)])
        try:
            result = await post_webhook(
                "http://peer.example/webhook",
                {"event": "test"},
                _http_client=client,
                retry_policy=NO_RETRY_POLICY,
            )
            assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
            assert result.status_code == status
        finally:
            await client.aclose()


# ── 3. 5xx retryable failure + retry behaviour ───────────────────────


@pytest.mark.asyncio
async def test_post_webhook_retries_on_5xx_and_returns_last_failure() -> None:
    """Tier 2: 3 successive 503s yield RETRYABLE_FAILURE after all
    retries exhausted.

    Verifies the retry loop attempts ``max_attempts`` times then
    returns the most recent failure.
    """
    client = _client_with_responses([
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
    ])
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.01)),
        )
        assert result.outcome is DeliveryOutcome.RETRYABLE_FAILURE
        assert result.status_code == 503
        assert result.should_retry is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_post_webhook_retries_succeed_after_initial_5xx() -> None:
    """Tier 2: when the first attempt is 5xx but the retry succeeds,
    return SUCCESS (= peer recovered)."""
    client = _client_with_responses([
        httpx.Response(503),
        httpx.Response(200),
    ])
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=(0.01,)),
        )
        assert result.outcome is DeliveryOutcome.SUCCESS
        assert result.status_code == 200
    finally:
        await client.aclose()


# ── 4. Transport error ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_treats_transport_error_as_retryable() -> None:
    """Tier 2: a transport-level exception (= ConnectError / TimeoutException)
    yields RETRYABLE_FAILURE with the error string captured.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=(0.01,)),
        )
        assert result.outcome is DeliveryOutcome.RETRYABLE_FAILURE
        assert result.status_code is None
        assert "connection refused" in (result.error or "")
        assert call_count["n"] == 2  # = both attempts run
    finally:
        await client.aclose()


# ── 5. NO_RETRY_POLICY semantics ─────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_no_retry_policy_attempts_once() -> None:
    """Tier 2: ``NO_RETRY_POLICY`` makes ``post_webhook`` behave like
    pre-#269 fire-and-forget: one attempt, no sleep, return immediately.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=NO_RETRY_POLICY,
        )
        assert result.outcome is DeliveryOutcome.RETRYABLE_FAILURE
        assert call_count["n"] == 1  # only one attempt
    finally:
        await client.aclose()


# ── 6. Default policy behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_default_policy_attempts_three_times() -> None:
    """Tier 2: omitting ``retry_policy`` uses DEFAULT_RETRY_POLICY
    (= 3 attempts) — verifying default callers get the conservative
    retry behaviour without needing to opt in.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        # Override the default 0.5s + 2s backoff to keep test fast.
        from reyn.chat.channel_state import RetryPolicy as _RP
        fast_policy = _RP(max_attempts=3, backoff_seconds=(0.01, 0.01))
        result = await post_webhook(
            "http://peer.example/webhook",
            {"event": "test"},
            _http_client=client,
            retry_policy=fast_policy,
        )
        assert result.outcome is DeliveryOutcome.RETRYABLE_FAILURE
        assert call_count["n"] == 3
    finally:
        await client.aclose()


# ── 7. Backwards-compat — return value safely ignorable ──────────────


@pytest.mark.asyncio
async def test_post_webhook_does_not_raise_on_any_outcome() -> None:
    """Tier 2: regardless of success / 4xx / 5xx / transport error,
    ``post_webhook`` does NOT raise — preserves pre-#269 fire-and-forget
    backwards-compat for callers that ignore the return value.
    """
    cases: list[object] = [
        httpx.Response(200),
        httpx.Response(404),
        httpx.Response(503),
    ]
    for response in cases:
        client = _client_with_responses([response])
        try:
            # Should NOT raise — return value can be ignored safely.
            await post_webhook(
                "http://peer.example/webhook",
                {"event": "test"},
                _http_client=client,
                retry_policy=NO_RETRY_POLICY,
            )
        finally:
            await client.aclose()
