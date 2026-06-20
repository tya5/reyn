"""Tier 2: device_grant_flow tolerates a malformed expires_in / interval.

The OAuth SERVER's device-authorization response is external. A non-RFC value
(``expires_in: null`` / non-numeric) hit ``int(auth_resp.get("expires_in", ...))``
→ opaque TypeError mid-login (the ``.get`` default only covers a *missing* key).
These are timing knobs (the grant is gated on ``access_token`` in the poll loop),
so the flow now coerces-to-default instead of crashing.

Policy: httpx.MockTransport Fake (a real httpx transport returning canned
responses — a Fake by the testing-policy taxonomy), real device_grant_flow +
EventLog. No MagicMock. Tier line first.
"""
from __future__ import annotations

from collections import deque

import httpx
import pytest

from reyn.core.events.events import EventLog
from reyn.security.secrets.oauth import (
    _REFRESH_LOCKS,
    OAuthProviderConfig,
    device_grant_flow,
)

_DEVICE_URL = "https://example.com/device"
_TOKEN_URL = "https://example.com/token"


@pytest.fixture(autouse=True)
def _clear_locks() -> None:
    _REFRESH_LOCKS.clear()


def _provider() -> OAuthProviderConfig:
    return OAuthProviderConfig(
        name="github",
        client_id="cid",
        device_authorization_url=_DEVICE_URL,
        token_url=_TOKEN_URL,
        scopes=["repo"],
        client_secret=None,
        audience=None,
    )


def _malformed_device_auth() -> dict:
    """RFC 8628 fields present EXCEPT expires_in / interval are null (= a
    non-compliant authorization server)."""
    return {
        "device_code": "abcd1234",
        "user_code": "WDJB-MJHT",
        "verification_uri": _DEVICE_URL,
        "expires_in": None,
        "interval": None,
    }


def _token_success() -> dict:
    return {
        "access_token": "AT_x",
        "refresh_token": "RT_x",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "repo",
    }


@pytest.mark.asyncio
async def test_malformed_expires_in_does_not_crash() -> None:
    """Tier 2: null expires_in/interval in the device-auth response → the flow
    still reaches the token grant (no opaque TypeError) and returns a valid token."""
    poll: deque = deque([httpx.Response(200, json=_token_success())])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_malformed_device_auth())
        return poll.popleft()

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        token = await device_grant_flow(
            _provider(),
            events=EventLog(),
            http_client=client,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    assert token.access_token == "AT_x"
