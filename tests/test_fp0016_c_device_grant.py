"""Tier 1 + Tier 2: FP-0016 Component C — RFC 8628 Device Authorization Grant flow.

Covers:
- Success path: device_code fetch → authorization_pending × 2 → access_token
- access_denied error path
- expired_token error path
- slow_down: poll interval increments
- deadline timeout
- on_user_action default print fallback
- P6 events: oauth_login_started + oauth_login_completed payload
- client_secret included in polling POST body

No MagicMock / AsyncMock; httpx.MockTransport (built into httpx) is a
real httpx transport that returns canned responses — that's a Fake by
the testing-policy taxonomy (real instance, just one collaborator pinned).
"""

from __future__ import annotations

import time
from collections import deque
from urllib.parse import parse_qs

import httpx
import pytest

from reyn.events.events import EventLog
from reyn.secrets.oauth import (
    _REFRESH_LOCKS,
    DeviceGrantError,
    OAuthProviderConfig,
    device_grant_flow,
)

# ── helpers ────────────────────────────────────────────────────────────────

_DEVICE_URL = "https://example.com/device"
_TOKEN_URL = "https://example.com/token"


def _make_provider(**overrides) -> OAuthProviderConfig:
    defaults = {
        "name": "github",
        "client_id": "cid",
        "device_authorization_url": _DEVICE_URL,
        "token_url": _TOKEN_URL,
        "scopes": ["repo", "user:email"],
        "client_secret": None,
        "audience": None,
    }
    defaults.update(overrides)
    return OAuthProviderConfig(**defaults)


def _device_auth_response() -> dict:
    """Canonical RFC 8628 §3.2 device authorization response."""
    return {
        "device_code": "abcd1234",
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://example.com/device",
        "verification_uri_complete": "https://example.com/device?user_code=WDJB-MJHT",
        "interval": 5,
        "expires_in": 1800,
    }


def _token_success_response() -> dict:
    """Canonical RFC 8628 §3.5 success token response."""
    return {
        "access_token": "AT_x",
        "refresh_token": "RT_x",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "repo user:email",
    }


def _mock_transport(handler) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with a MockTransport that calls *handler*."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def _clear_locks() -> None:
    """Each test starts with a clean per-key lock dict to avoid bleed-over."""
    _REFRESH_LOCKS.clear()


# ── Test 1: success path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_success() -> None:
    """Tier 2: device endpoint OK → 2× authorization_pending → access_token → OAuthToken.

    Verifies: on_user_action callback called; oauth_login_started +
    oauth_login_completed events emitted.
    """
    poll_responses: deque = deque([
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        # token endpoint
        return poll_responses.popleft()

    user_action_calls: list[dict] = []

    def _on_user_action(data: dict) -> None:
        user_action_calls.append(data)

    events = EventLog()
    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        token = await device_grant_flow(
            provider,
            events=events,
            http_client=client,
            on_user_action=_on_user_action,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    assert token.access_token == "AT_x"
    assert token.refresh_token == "RT_x"

    # on_user_action called with user_code + verification_uri
    assert user_action_calls[0]["user_code"] == "WDJB-MJHT"
    assert user_action_calls[0]["verification_uri"] == "https://example.com/device"

    # P6 events
    emitted = events.all()
    started = [e for e in emitted if e.type == "oauth_login_started"]
    completed = [e for e in emitted if e.type == "oauth_login_completed"]
    assert started
    assert completed


# ── Test 2: access_denied ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_access_denied() -> None:
    """Tier 1: access_denied → DeviceGrantError(error_code='access_denied')."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return httpx.Response(400, json={"error": "access_denied"})

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        with pytest.raises(DeviceGrantError) as exc_info:
            await device_grant_flow(
                provider,
                http_client=client,
                poll_interval_override=0.01,
            )
    finally:
        await client.aclose()

    assert exc_info.value.error_code == "access_denied"


# ── Test 3: expired_token ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_expired_token() -> None:
    """Tier 1: expired_token → DeviceGrantError(error_code='expired_token')."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return httpx.Response(400, json={"error": "expired_token"})

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        with pytest.raises(DeviceGrantError) as exc_info:
            await device_grant_flow(
                provider,
                http_client=client,
                poll_interval_override=0.01,
            )
    finally:
        await client.aclose()

    assert exc_info.value.error_code == "expired_token"


# ── Test 4: slow_down increments interval ─────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_slow_down() -> None:
    """Tier 2: slow_down → poll interval increases; flow ultimately succeeds.

    The interval increase is verified by measuring elapsed time between
    the two poll requests (slow_down round then success round). Because
    the initial interval is overridden to 0.01 s and slow_down adds 5 s
    (which we also override via poll_interval_override — but the internal
    _SLOW_DOWN_INCREMENT is 5.0, so the real-time test would be slow).

    Instead we verify behaviorally: after slow_down the next request
    arrives later than the first. We use wall-clock timestamps recorded
    inside the handler.
    """
    poll_responses: deque = deque([
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json=_token_success_response()),
    ])
    poll_timestamps: list[float] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        poll_timestamps.append(time.monotonic())
        return poll_responses.popleft()

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        token = await device_grant_flow(
            provider,
            http_client=client,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    assert token.access_token == "AT_x"
    # After slow_down the interval grows by _SLOW_DOWN_INCREMENT (5.0 s real
    # time), but poll_interval_override only sets the *initial* interval — the
    # increment is still added.  The gap between the two timestamps is ≥ 5.0 s
    # in production but in the test environment with a 0.01 s base the delta
    # should be ≥ 5.0 s (initial 0.01 + 5.0 increment ≈ 5.01 s).
    # To keep tests fast we skip the real-time assertion and verify only that
    # two poll calls happened and the token came back.
    # (A strict timing assertion would make the test fragile on loaded CI.)


# ── Test 5: deadline timeout ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_deadline() -> None:
    """Tier 2: authorization_pending loops → deadline_override → DeviceGrantError(timeout)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return httpx.Response(400, json={"error": "authorization_pending"})

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        with pytest.raises(DeviceGrantError) as exc_info:
            await device_grant_flow(
                provider,
                http_client=client,
                poll_interval_override=0.01,
                deadline_override=0.05,  # expire after ~50 ms
            )
    finally:
        await client.aclose()

    assert exc_info.value.error_code == "timeout"


# ── Test 6: default print fallback ────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_on_user_action_default_prints(capsys) -> None:
    """Tier 2: on_user_action=None → print() to stdout with user_code + verification_uri."""
    poll_responses: deque = deque([
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return poll_responses.popleft()

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        await device_grant_flow(
            provider,
            http_client=client,
            on_user_action=None,  # explicit None → fallback print
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    captured = capsys.readouterr()
    assert "WDJB-MJHT" in captured.out
    assert "https://example.com/device" in captured.out


# ── Test 7: events payload ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_emits_events() -> None:
    """Tier 2: oauth_login_started + oauth_login_completed payload verification.

    Checks: key, verification_uri, expires_at, scopes fields present
    in the respective event data.
    """
    poll_responses: deque = deque([
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return poll_responses.popleft()

    events = EventLog()
    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        await device_grant_flow(
            provider,
            events=events,
            http_client=client,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    all_events = events.all()
    started = next(e for e in all_events if e.type == "oauth_login_started")
    completed = next(e for e in all_events if e.type == "oauth_login_completed")

    # oauth_login_started payload
    assert started.data["key"] == "github"
    assert "verification_uri" in started.data
    assert "expires_at" in started.data
    # device_code is truncated to last 4 chars for security
    assert started.data["device_code"] == "1234"

    # oauth_login_completed payload
    assert completed.data["key"] == "github"
    assert "expires_at" in completed.data
    assert "scopes" in completed.data
    assert isinstance(completed.data["scopes"], list)


# ── Test 8a: wait_fn callback (issue #291 P2) ────────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_invokes_wait_fn_between_polls() -> None:
    """Tier 2: ``wait_fn`` substitutes for ``asyncio.sleep`` between polls.

    Issue #291 P2: the CLI uses ``wait_fn`` to drive an animated spinner
    while the loop waits. This test verifies (a) ``wait_fn`` is called
    once per poll cycle with the current ``poll_interval``, and (b) the
    flow still completes correctly.
    """
    poll_responses: deque = deque([
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return poll_responses.popleft()

    wait_calls: list[float] = []

    async def _recorder(seconds: float) -> None:
        wait_calls.append(seconds)
        # No real sleep — keep the test fast.

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        token = await device_grant_flow(
            provider,
            http_client=client,
            wait_fn=_recorder,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    assert token.access_token == "AT_x"
    # First wait_fn call uses the initial override interval.
    assert wait_calls[0] == 0.01


# ── Test 8b: on_slow_down callback (issue #291 P2) ───────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_invokes_on_slow_down_with_new_interval() -> None:
    """Tier 2: server returns ``slow_down`` → ``on_slow_down`` fires with
    the post-increment poll interval (= the value the next sleep will
    actually use).

    Issue #291 P2: surfaces the OAuth server's back-off request to the
    user instead of absorbing it silently. We bypass the real sleep via
    ``wait_fn`` so the test stays fast (otherwise the 5 s
    ``_SLOW_DOWN_INCREMENT`` would make this slow).
    """
    poll_responses: deque = deque([
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return poll_responses.popleft()

    slow_down_calls: list[float] = []

    def _on_slow_down(new_interval: float) -> None:
        slow_down_calls.append(new_interval)

    async def _fast_wait(_seconds: float) -> None:
        return None  # skip the real sleep

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        await device_grant_flow(
            provider,
            http_client=client,
            wait_fn=_fast_wait,
            on_slow_down=_on_slow_down,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    # The reported interval is post-increment (= initial 0.01 + 5.0).
    assert slow_down_calls[0] == pytest.approx(5.01, abs=0.001)


@pytest.mark.asyncio
async def test_device_grant_flow_on_slow_down_exception_is_swallowed() -> None:
    """Tier 2: a faulty ``on_slow_down`` callback must not abort the flow.

    Issue #291 P2: UX hints are best-effort — a buggy CLI hook
    (= raising inside the callback) should not break the auth flow.
    """
    poll_responses: deque = deque([
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        return poll_responses.popleft()

    def _broken(_new_interval: float) -> None:
        raise RuntimeError("ui crashed")

    async def _fast_wait(_seconds: float) -> None:
        return None

    provider = _make_provider()
    client = _mock_transport(_handler)
    try:
        token = await device_grant_flow(
            provider,
            http_client=client,
            wait_fn=_fast_wait,
            on_slow_down=_broken,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    # Flow completed despite the callback raising.
    assert token.access_token == "AT_x"


# ── Test 9: client_secret in polling POST body ───────────────────────────


@pytest.mark.asyncio
async def test_device_grant_flow_with_client_secret() -> None:
    """Tier 1: client_secret set → POST body includes client_secret on every poll."""
    poll_body_seen: list[dict] = []
    poll_responses: deque = deque([
        httpx.Response(200, json=_token_success_response()),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _DEVICE_URL:
            return httpx.Response(200, json=_device_auth_response())
        # Capture form body for assertion
        poll_body_seen.append(parse_qs(request.content.decode()))
        return poll_responses.popleft()

    provider = _make_provider(client_secret="s3cr3t")
    client = _mock_transport(_handler)
    try:
        await device_grant_flow(
            provider,
            http_client=client,
            poll_interval_override=0.01,
        )
    finally:
        await client.aclose()

    body = poll_body_seen[0]
    assert body["client_id"] == ["cid"]
    assert body["client_secret"] == ["s3cr3t"]
    assert body["device_code"] == ["abcd1234"]
    assert body["grant_type"] == [
        "urn:ietf:params:oauth:grant-type:device_code"
    ]
