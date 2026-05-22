"""Tier 2: Slack inbound webhook router — FP-0041 #489 PR-D.

Tests the ``/webhook/slack`` route + the helpers it depends on:

  ``verify_slack_signature`` — HMAC-SHA256 + replay window
  ``mint_envelope_from_slack_event`` — Slack payload → Reyn envelope
  POST /webhook/slack — full route flow (URL verification / signing /
    event dispatch / target agent resolution / inbox push)

The actual end-to-end "Slack message reaches LLM via agent inbox" is
exercised via FastAPI TestClient + a registry mock that captures
inbox pushes. The signing helper is unit-tested directly so the
crypto path is verified independent of the route plumbing.

Tier 2 because the webhook is the **only entry point** for Slack
messages reaching Reyn. A regression in signing / parsing / inbox
push silently breaks the entire Slack chat-transport.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

from reyn.chat.transport import ExternalRef
from reyn.plugins.sample_slack.webhook import (
    _SLACK_REPLAY_WINDOW_SECONDS,
    build_router,
    mint_envelope_from_slack_event,
    verify_slack_signature,
)

# ── verify_slack_signature ────────────────────────────────────────────


def _sign(body: bytes, ts: str, secret: str) -> str:
    """Helper: produce the Slack-format signature for body + timestamp."""
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_verify_signature_accepts_well_formed_request():
    """Tier 2: a request signed with the correct secret + fresh timestamp
    passes verification.
    """
    secret = "test-secret"
    body = b'{"hello": "world"}'
    ts = str(int(time.time()))
    sig = _sign(body, ts, secret)

    ok, detail = verify_slack_signature(
        body=body, timestamp=ts, signature=sig, signing_secret=secret,
    )
    assert ok is True
    assert detail == "ok"


def test_verify_signature_rejects_stale_timestamp():
    """Tier 2: a request older than the replay window is rejected as
    ``stale`` — protects against replay attacks.
    """
    secret = "test-secret"
    body = b'{"hello": "world"}'
    # Stale timestamp = 10 minutes ago.
    ts = str(int(time.time()) - 600)
    sig = _sign(body, ts, secret)

    ok, detail = verify_slack_signature(
        body=body, timestamp=ts, signature=sig, signing_secret=secret,
    )
    assert ok is False
    assert detail == "stale"


def test_verify_signature_rejects_signature_mismatch():
    """Tier 2: signature computed with a different secret fails the
    constant-time compare with ``mismatch`` detail.
    """
    body = b'{"x":1}'
    ts = str(int(time.time()))
    # Sign with the wrong secret.
    bad_sig = _sign(body, ts, "wrong-secret")

    ok, detail = verify_slack_signature(
        body=body, timestamp=ts, signature=bad_sig,
        signing_secret="real-secret",
    )
    assert ok is False
    assert detail == "mismatch"


def test_verify_signature_rejects_missing_headers():
    """Tier 2: missing timestamp or signature returns a clear reason."""
    secret = "s"

    ok, detail = verify_slack_signature(
        body=b"", timestamp="", signature="sig", signing_secret=secret,
    )
    assert ok is False
    assert detail == "missing-timestamp"

    ok, detail = verify_slack_signature(
        body=b"", timestamp="1234", signature="", signing_secret=secret,
    )
    assert ok is False
    assert detail == "missing-signature"


def test_verify_signature_replay_window_boundary_inclusive():
    """Tier 2: at exactly the window boundary (= 5 minutes), the
    request still passes; one second past, it's stale. Pins the
    boundary semantics.
    """
    secret = "s"
    body = b""
    now = 1_000_000.0
    ts_at_boundary = str(int(now) - _SLACK_REPLAY_WINDOW_SECONDS)
    sig = _sign(body, ts_at_boundary, secret)
    ok, _ = verify_slack_signature(
        body=body, timestamp=ts_at_boundary, signature=sig,
        signing_secret=secret, now=now,
    )
    assert ok is True

    ts_past = str(int(now) - _SLACK_REPLAY_WINDOW_SECONDS - 1)
    sig = _sign(body, ts_past, secret)
    ok, detail = verify_slack_signature(
        body=body, timestamp=ts_past, signature=sig,
        signing_secret=secret, now=now,
    )
    assert ok is False
    assert detail == "stale"


# ── mint_envelope_from_slack_event ────────────────────────────────────


def test_mint_envelope_for_app_mention():
    """Tier 2: an ``app_mention`` event mints an envelope with
    sender=slack:<user_id> and ExternalRef reply_to carrying
    channel + thread_ts.
    """
    event = {
        "event": {
            "type": "app_mention",
            "user": "U456",
            "text": "<@U_BOT> help me",
            "channel": "C123",
            "ts": "1234.5678",
        },
    }
    env = mint_envelope_from_slack_event(event)
    assert env is not None
    assert env["text"] == "<@U_BOT> help me"
    assert env["sender"] == "slack:U456"
    rt = env["reply_to"]
    assert isinstance(rt, ExternalRef)
    assert rt.transport == "slack"
    assert rt.destination == {"channel": "C123", "thread_ts": "1234.5678"}


def test_mint_envelope_preserves_existing_thread_ts():
    """Tier 2: when the user replies inside an existing thread, the
    envelope's reply_to.thread_ts is the thread's ts, NOT the
    message's own ts — so the agent reply joins the right thread.
    """
    event = {
        "event": {
            "type": "message",
            "user": "U456",
            "text": "follow-up",
            "channel": "C123",
            "ts": "9999.0001",
            "thread_ts": "1234.5678",  # original parent
        },
    }
    env = mint_envelope_from_slack_event(event)
    assert env is not None
    assert env["reply_to"].destination["thread_ts"] == "1234.5678"


def test_mint_envelope_skips_bot_echo():
    """Tier 2: a ``bot_message`` subtype (= echo of bot's own post) is
    NOT dispatched to inbox — prevents feedback loops where the bot
    receives its own messages.
    """
    event = {
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "bot_id": "B999",
            "text": "I (the bot) just posted",
            "channel": "C123",
            "ts": "1.0",
        },
    }
    assert mint_envelope_from_slack_event(event) is None


def test_mint_envelope_skips_non_message_events():
    """Tier 2: reaction / channel_join / app_home events produce no
    envelope. Operator can subscribe to broad event scopes without
    risking accidental dispatch.
    """
    for event_type in ("reaction_added", "channel_join", "app_home_opened"):
        event = {"event": {"type": event_type, "user": "U1", "channel": "C1"}}
        assert mint_envelope_from_slack_event(event) is None


def test_mint_envelope_skips_empty_text():
    """Tier 2: an event with no text (= whitespace or missing) is
    skipped. Nothing to dispatch to the LLM.
    """
    event = {
        "event": {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "ts": "1.0",
            "text": "   ",
        },
    }
    assert mint_envelope_from_slack_event(event) is None


def test_mint_envelope_handles_missing_user_field():
    """Tier 2: events without a ``user`` (= system messages, channel
    purpose updates) get sender=slack:unknown but still dispatch
    if they have text — defensive about Slack edge cases.
    """
    event = {
        "event": {
            "type": "message",
            "text": "hi",
            "channel": "C1",
            "ts": "1.0",
        },
    }
    env = mint_envelope_from_slack_event(event)
    assert env is not None
    assert env["sender"] == "slack:unknown"


# ── route end-to-end via TestClient ───────────────────────────────────


@pytest.fixture()
def _slack_client(monkeypatch):
    """FastAPI TestClient with a stubbed AgentRegistry.

    Builds a fresh minimal FastAPI app, mounts the sample_slack
    plugin's router via ``build_router``, and overrides the registry
    dependency to capture inbox pushes. Each test gets its own client
    (= no shared state across tests). Avoids loading the full
    ``reyn.web.server.app`` since the plugin would otherwise be
    mounted only when the operator activates it via reyn.yaml.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    pushes: list = []

    class _StubSession:
        async def _put_inbox(self, kind, payload):
            pushes.append((kind, payload))
            return "stub-msg-id"

    class _StubRegistry:
        async def ensure_running(self, name):
            return _StubSession()

    def _stub_get_registry():
        return _StubRegistry()

    # Patch the deps module so build_router's dependency closure
    # resolves to the stub.
    from reyn.web import deps
    monkeypatch.setattr(deps, "_get_registry", _stub_get_registry)
    monkeypatch.setattr(deps, "_registry", None, raising=False)

    app = FastAPI()
    router = build_router(target_agent="news_agent")
    app.include_router(router)
    app.dependency_overrides[deps.get_registry] = _stub_get_registry

    client = TestClient(app)
    client.pushes = pushes  # type: ignore[attr-defined]
    yield client


def test_route_url_verification_echoes_challenge(_slack_client):
    """Tier 2 (#489 PR-D): the Slack URL verification handshake echoes
    the challenge string back so api.slack.com can verify Reyn is
    reachable + responding. This MUST work without signing — Slack
    doesn't always sign the initial verify.
    """
    body = json.dumps({
        "type": "url_verification",
        "challenge": "abc123-test-challenge",
    })
    response = _slack_client.post(
        "/webhook/slack",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123-test-challenge"}


def test_route_rejects_missing_signing_secret(monkeypatch, _slack_client):
    """Tier 2: with no ``SLACK_SIGNING_SECRET`` env var set, the
    route returns 503 instead of crashing. Operator forgot to
    configure → clear error in logs.
    """
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    body = json.dumps({
        "event": {
            "type": "app_mention", "user": "U1", "channel": "C1",
            "text": "hi", "ts": "1.0",
        },
    })
    response = _slack_client.post(
        "/webhook/slack",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 503
    assert "signing secret" in response.json()["error"].lower()


def test_route_rejects_bad_signature(monkeypatch, _slack_client):
    """Tier 2: a request with the wrong signature returns 401. Even
    if the body looks like a valid event, Reyn won't dispatch.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "real-secret")
    monkeypatch.setenv("SLACK_TARGET_AGENT", "test_agent")
    body = json.dumps({
        "event": {
            "type": "app_mention", "user": "U1", "channel": "C1",
            "text": "hi", "ts": "1.0",
        },
    }).encode()
    ts = str(int(time.time()))
    bad_sig = _sign(body, ts, "wrong-secret")
    response = _slack_client.post(
        "/webhook/slack",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": bad_sig,
        },
    )
    assert response.status_code == 401


def test_route_dispatches_signed_app_mention_to_inbox(
    monkeypatch, _slack_client,
):
    """Tier 2 (#489 PR-D end-to-end): a properly-signed ``app_mention``
    event reaches the target agent's inbox with the right envelope.

    This is the **happy path** for the Slack chat-transport: Slack
    user @mentions the bot → Reyn webhook verifies signing → mints
    envelope with sender + ExternalRef reply_to → pushes to inbox.
    PR-A dispatch attribution + LLM processing kick in from there.
    """
    secret = "real-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    monkeypatch.setenv("SLACK_TARGET_AGENT", "news_agent")

    body = json.dumps({
        "event": {
            "type": "app_mention",
            "user": "U456",
            "text": "<@U_BOT> today's news?",
            "channel": "C123",
            "ts": "1234.5678",
        },
    }).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts, secret)

    response = _slack_client.post(
        "/webhook/slack",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # One inbox push happened with the expected envelope.
    pushes = _slack_client.pushes
    assert len(pushes) == 1
    kind, payload = pushes[0]
    assert kind == "user"
    assert payload["text"] == "<@U_BOT> today's news?"
    assert payload["sender"] == "slack:U456"
    rt = payload["reply_to"]
    assert isinstance(rt, ExternalRef)
    assert rt.transport == "slack"
    assert rt.destination == {"channel": "C123", "thread_ts": "1234.5678"}


def test_route_acks_non_dispatchable_events_with_200(
    monkeypatch, _slack_client,
):
    """Tier 2: an event with no dispatchable payload (= reaction)
    returns 200 with ``status="ignored"``. Slack won't retry.
    """
    secret = "real-secret"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)

    body = json.dumps({
        "event": {
            "type": "reaction_added", "user": "U1", "channel": "C1",
        },
    }).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts, secret)

    response = _slack_client.post(
        "/webhook/slack",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert len(_slack_client.pushes) == 0


def test_route_malformed_json_returns_400(monkeypatch, _slack_client):
    """Tier 2: non-JSON body returns 400 (= operator misconfig / bad
    network, not Reyn's fault).
    """
    response = _slack_client.post(
        "/webhook/slack",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


# ── register_router (= plugin entry-point) ────────────────────────────


def test_register_router_returns_none_when_target_agent_missing(monkeypatch):
    """Tier 2: ``register_router`` (= the plugin entry-point) returns
    ``None`` when ``target_agent`` is missing from config — the
    loader logs a warning and the route is not mounted. Operator's
    forgotten config surfaces in logs rather than crashing.
    """
    from reyn.plugins.sample_slack import register_router
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")  # signing OK, target missing

    assert register_router({}) is None
    assert register_router({"target_agent": ""}) is None


def test_register_router_returns_none_when_signing_secret_missing(monkeypatch):
    """Tier 2: ``register_router`` returns ``None`` when
    ``SLACK_SIGNING_SECRET`` env var is unset — clear operator
    error in logs without crashing reyn web.
    """
    from reyn.plugins.sample_slack import register_router
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    assert register_router({"target_agent": "news_agent"}) is None


def test_register_router_returns_apirouter_when_configured(monkeypatch):
    """Tier 2: ``register_router`` returns a FastAPI ``APIRouter``
    when both ``target_agent`` config + signing secret env are
    present. The loader mounts this on the app.
    """
    from fastapi import APIRouter

    from reyn.plugins.sample_slack import register_router
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")

    router = register_router({"target_agent": "news_agent"})
    assert isinstance(router, APIRouter)
