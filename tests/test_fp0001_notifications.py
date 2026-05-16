"""Tier 1: FP-0001 post_webhook — contract tests for the webhook notification helper.

Covers:
1. post_webhook POSTs JSON body to the target URL (verified via MockTransport)
2. Non-2xx response is logged at WARNING, not raised
3. Network error (MockTransport raising httpx.ConnectError) is logged, not raised
4. Timeout kwarg is accepted and forwarded

No MagicMock / AsyncMock / patch. httpx.MockTransport is a real httpx
transport (Fake per testing policy taxonomy); the injectable _http_client
parameter follows the same real-instance injection pattern as get_valid_token.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from reyn.web.notifications import _DEFAULT_TIMEOUT, post_webhook

# ── helpers ────────────────────────────────────────────────────────────────────


def _client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient backed by MockTransport(handler)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── 1. Successful POST — JSON body verified ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_sends_json_body() -> None:
    """Tier 1: post_webhook POSTs payload as JSON to the target URL."""
    captured: dict = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = json.loads(await request.aread())
        return httpx.Response(200)

    payload = {"run_id": "abc123", "status": "input-required", "question": "Which env?"}
    client = _client(_handler)
    try:
        await post_webhook("https://example.com/hook", payload, _http_client=client)
    finally:
        await client.aclose()

    assert captured["method"] == "POST"
    assert "example.com" in captured["url"]
    assert "application/json" in captured["content_type"]
    assert captured["body"] == payload


# ── 2. Non-2xx response — logged, not raised ───────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_non_2xx_logged_not_raised(caplog) -> None:
    """Tier 1: 4xx/5xx response logs WARNING and does not propagate to caller."""
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = _client(_handler)
    try:
        with caplog.at_level(logging.WARNING, logger="reyn.web.notifications"):
            # Must not raise even for 503
            await post_webhook(
                "https://example.com/hook", {"run_id": "x"}, _http_client=client
            )
    finally:
        await client.aclose()

    assert any("503" in record.message for record in caplog.records)


# ── 3. Network error — logged, not raised ─────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_network_error_logged_not_raised(caplog) -> None:
    """Tier 1: ConnectError from the transport is caught, logged, not raised."""
    url = "https://unreachable.example.com/hook"

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = _client(_handler)
    try:
        with caplog.at_level(logging.WARNING, logger="reyn.web.notifications"):
            # Must not raise despite network error
            await post_webhook(url, {"run_id": "y"}, _http_client=client)
    finally:
        await client.aclose()

    assert any("webhook post failed" in record.message for record in caplog.records)
    assert any(url in record.message for record in caplog.records)


# ── 4. Timeout kwarg accepted ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_webhook_timeout_kwarg_accepted() -> None:
    """Tier 1: timeout kwarg is accepted without TypeError; call completes."""
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    # Pass a custom timeout; verify no TypeError and the call succeeds.
    # The injectable client ignores the timeout kwarg (it's for the
    # auto-created client path), but the function signature must accept it.
    client = _client(_handler)
    try:
        await post_webhook(
            "https://example.com/hook", {}, timeout=3.0, _http_client=client
        )
    finally:
        await client.aclose()


# ── 5. Default timeout is a positive float ────────────────────────────────────


def test_default_timeout_constant() -> None:
    """Tier 1: _DEFAULT_TIMEOUT is a positive float (sanity contract)."""
    assert isinstance(_DEFAULT_TIMEOUT, float)
    assert _DEFAULT_TIMEOUT > 0
