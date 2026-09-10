"""Tier 2: #5990 (part of, stage 2) — `oauth._poll_token`'s own `except
ValueError: body = {}` swallowed a non-JSON response from the OAuth
token endpoint with no visible report. Now warns (status + URL only,
never the response BODY -- a token endpoint's non-JSON reply can
legitimately be or embed sensitive text) before falling back to an
empty body, same convention #6038 established.

Policy: `httpx.MockTransport` Fake (a real httpx transport returning
canned responses), real `_poll_token`. No MagicMock.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from reyn.security.secrets.oauth import OAuthProviderConfig, _poll_token

_LOGGER_NAME = "reyn.security.secrets.oauth"
_TOKEN_URL = "https://example.com/token"


def _provider() -> OAuthProviderConfig:
    return OAuthProviderConfig(
        name="github",
        client_id="cid",
        device_authorization_url="https://example.com/device",
        token_url=_TOKEN_URL,
        scopes=["repo"],
        client_secret=None,
        audience=None,
    )


@pytest.mark.asyncio
async def test_non_json_token_response_warns_and_falls_back_to_empty_body(caplog) -> None:
    """Tier 2: a non-JSON token-endpoint response warns (status + URL, no
    body content) AND `_poll_token` still returns `(status, {})` rather
    than raising.

    Strip-falsify (verified by hand: the `_log.warning(...)` call removed
    from `_poll_token`): this test goes red — no record at all."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            status, body = await _poll_token(_provider(), "device123", client)
    finally:
        await client.aclose()
    assert status == 502
    assert body == {}
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert any(_TOKEN_URL in r.message and "502" in r.message for r in records)
    assert not any("Bad Gateway" in r.message for r in records), (
        "the response body itself must never be logged -- it can carry sensitive text"
    )


@pytest.mark.asyncio
async def test_well_formed_json_token_response_does_not_warn(caplog) -> None:
    """Tier 2: negative control -- a well-formed JSON response parses
    through with no warning at all."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "AT_x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            status, body = await _poll_token(_provider(), "device123", client)
    finally:
        await client.aclose()
    assert status == 200
    assert body == {"access_token": "AT_x"}
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
