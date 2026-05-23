"""Tier 2: sample_line plugin (FP-0041 plugins-api PR-2).

The hand-rolled webhook handler that landed in PR #524 has been
replaced with ``line-bot-sdk`` v3. The SDK handles signature
verification + event parsing; the plugin glues SDK events → Reyn
``push_to_agent``.

Tests focus on:

  1. ``register_router`` entry-point opt-out paths.
  2. ``build_router`` mounts ``/webhook/line``.
  3. End-to-end via TestClient + signed body + stubbed registry:
     - User-source text message → envelope with user-scoped sender
     - Group-source → group-scoped sender + group ``source_id``
     - Room-source → room-scoped sender + room ``source_id``
     - Non-text / non-message events skip dispatch
     - Bad signature → 401
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

linebot = pytest.importorskip("linebot.v3")


def _sign(body: bytes, secret: str) -> str:
    """Build LINE's X-Line-Signature header value."""
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest(),
    ).decode()


# ── register_router entry-point ────────────────────────────────────────


def test_register_router_returns_none_when_target_agent_missing(monkeypatch):
    """Tier 2: register_router skips when target_agent absent."""
    from reyn.plugins.sample_line import register_router
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "x")
    assert register_router({}) is None
    assert register_router({"target_agent": ""}) is None


def test_register_router_returns_none_when_channel_secret_missing(monkeypatch):
    """Tier 2: register_router skips when channel secret env unset."""
    from reyn.plugins.sample_line import register_router
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    assert register_router({"target_agent": "x"}) is None


def test_register_router_returns_apirouter_when_configured(monkeypatch):
    """Tier 2: with both present, returns an APIRouter."""
    from fastapi import APIRouter

    from reyn.plugins.sample_line import register_router
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "x")
    router = register_router({"target_agent": "line_agent"})
    assert isinstance(router, APIRouter)


# ── route mount ───────────────────────────────────────────────────────


def test_build_router_mounts_webhook_line_path(monkeypatch):
    """Tier 2: the router exposes POST ``/webhook/line``."""
    from fastapi import FastAPI

    from reyn.plugins.sample_line.webhook import build_router
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "x")
    app = FastAPI()
    app.include_router(build_router(target_agent="line_agent"))
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/webhook/line" in paths


# ── end-to-end via TestClient + stubbed registry ──────────────────────


@pytest.fixture()
def _line_client(monkeypatch):
    """FastAPI TestClient with stubbed registry; captures pushes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    pushed: list = []

    class _StubSession:
        async def _put_inbox(self, kind, payload):
            pushed.append((kind, payload))
            return "stub-msg-id"

    class _StubRegistry:
        async def ensure_running(self, name):
            return _StubSession()

        def list_names(self):
            return ["line_agent"]

        def exists(self, name):
            return name == "line_agent"

    from reyn.web import deps
    monkeypatch.setattr(deps, "_get_registry", lambda: _StubRegistry())
    monkeypatch.setattr(deps, "_registry", None, raising=False)
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "channel-secret")

    from reyn.plugins.sample_line.webhook import build_router
    app = FastAPI()
    app.include_router(build_router(target_agent="line_agent"))

    client = TestClient(app)
    client.pushed = pushed  # type: ignore[attr-defined]
    yield client


def _post_signed(client, secret: str, payload: dict):
    body = json.dumps(payload).encode()
    sig = _sign(body, secret)
    return client.post(
        "/webhook/line",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )


def _user_text_event(text: str, user_id: str = "U456",
                      reply_token: str = "TOK1") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": user_id},
        "message": {
            "type": "text",
            "id": "M1",
            "text": text,
            "quoteToken": "Q1",
        },
        "timestamp": 1234,
        "mode": "active",
        "webhookEventId": "EV1",
        "deliveryContext": {"isRedelivery": False},
    }


def test_user_text_message_dispatches_to_agent(_line_client):
    """Tier 2 end-to-end: a 1:1 user text message reaches the agent
    with sender=line:user:<id>.
    """
    response = _post_signed(_line_client, "channel-secret", {
        "destination": "U-bot",
        "events": [_user_text_event("hello LINE bot")],
    })
    assert response.status_code == 200
    pushed = _line_client.pushed
    assert len(pushed) == 1
    kind, payload = pushed[0]
    assert kind == "user"
    assert payload["text"] == "hello LINE bot"
    assert payload["sender"] == "line:user:U456"

    from reyn.chat.transport import ExternalRef
    assert isinstance(payload["reply_to"], ExternalRef)
    assert payload["reply_to"].transport == "line"
    assert payload["reply_to"].destination["reply_token"] == "TOK1"
    assert payload["reply_to"].destination["source_type"] == "user"
    assert payload["reply_to"].destination["source_id"] == "U456"


def test_group_source_text_message(_line_client):
    """Tier 2: group source produces line:group:<groupId>:<userId>."""
    event = {
        "type": "message",
        "replyToken": "TOK2",
        "source": {"type": "group", "groupId": "G999", "userId": "U456"},
        "message": {
            "type": "text", "id": "M2", "text": "hi", "quoteToken": "Q2",
        },
        "timestamp": 1234, "mode": "active", "webhookEventId": "EV2",
        "deliveryContext": {"isRedelivery": False},
    }
    response = _post_signed(_line_client, "channel-secret", {
        "destination": "U-bot",
        "events": [event],
    })
    assert response.status_code == 200
    _, payload = _line_client.pushed[0]
    assert payload["sender"] == "line:group:G999:U456"
    assert payload["reply_to"].destination["source_id"] == "G999"


def test_room_source_text_message(_line_client):
    """Tier 2: room source produces line:room:<roomId>:<userId>."""
    event = {
        "type": "message",
        "replyToken": "TOK3",
        "source": {"type": "room", "roomId": "R777", "userId": "U456"},
        "message": {
            "type": "text", "id": "M3", "text": "hi room", "quoteToken": "Q3",
        },
        "timestamp": 1234, "mode": "active", "webhookEventId": "EV3",
        "deliveryContext": {"isRedelivery": False},
    }
    response = _post_signed(_line_client, "channel-secret", {
        "destination": "U-bot",
        "events": [event],
    })
    assert response.status_code == 200
    _, payload = _line_client.pushed[0]
    assert payload["sender"] == "line:room:R777:U456"
    assert payload["reply_to"].destination["source_id"] == "R777"


def test_non_text_message_skipped(_line_client):
    """Tier 2: sticker / image / etc. messages aren't dispatched
    as text turns. line-bot-sdk parses them as separate Content
    types; our handler only dispatches TextMessageContent.
    """
    event = {
        "type": "message",
        "replyToken": "TOK4",
        "source": {"type": "user", "userId": "U1"},
        "message": {
            "type": "sticker",
            "id": "M4",
            "packageId": "1",
            "stickerId": "1",
            "stickerResourceType": "STATIC",
            "quoteToken": "Q4",
        },
        "timestamp": 1234, "mode": "active", "webhookEventId": "EV4",
        "deliveryContext": {"isRedelivery": False},
    }
    response = _post_signed(_line_client, "channel-secret", {
        "destination": "U-bot",
        "events": [event],
    })
    assert response.status_code == 200
    assert _line_client.pushed == []


def test_bad_signature_returns_401(_line_client):
    """Tier 2: an incorrectly-signed body → 401, no inbox push."""
    body = json.dumps({"destination": "U-bot", "events": []}).encode()
    bad_sig = _sign(body, "wrong-secret")
    response = _line_client.post(
        "/webhook/line",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": bad_sig,
        },
    )
    assert response.status_code == 401
    assert _line_client.pushed == []


def test_empty_events_array_returns_ok(_line_client):
    """Tier 2: an empty events array (= LINE verify ping) returns
    200 ``dispatched=0`` without crashing.
    """
    response = _post_signed(_line_client, "channel-secret", {
        "destination": "U-bot",
        "events": [],
    })
    assert response.status_code == 200
    assert response.json()["dispatched"] == 0
    assert _line_client.pushed == []
