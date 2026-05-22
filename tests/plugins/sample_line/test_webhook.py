"""Tier 2: sample_line plugin (FP-0041 #489 PR-E).

LINE Messaging API webhook handler — mirror of ``sample_slack``
with LINE-specific protocol (= base64 HMAC, events array, source
type variants, replyToken).

Tests:

  1. ``verify_line_signature`` — base64 HMAC matches, mismatch,
     missing header, channel secret rotation
  2. ``mint_envelope_from_line_event``: text message / user / group /
     room source / non-text events skipped / non-message skipped /
     replyToken forwarded
  3. ``register_router`` entry point — missing target_agent /
     missing channel secret → None; happy path → APIRouter
  4. Route via TestClient + stubbed registry: signed valid event →
     inbox push; bad signature → 401; LINE verify ping (= events
     [] or absent) → 200 ignored

Tier 2 because the plugin is the only inbound path for LINE chat-
transport; a regression in signing / parsing / dispatch silently
breaks the integration.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reyn.chat.transport import ExternalRef
from reyn.plugins.sample_line.webhook import (
    build_router,
    mint_envelope_from_line_event,
    verify_line_signature,
)


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest(),
    ).decode()


# ── verify_line_signature ─────────────────────────────────────────────


def test_verify_signature_accepts_well_formed():
    """Tier 2: a base64-encoded HMAC-SHA256 of the body matches."""
    secret = "channel-secret"
    body = b'{"events": []}'
    sig = _sign(body, secret)
    ok, detail = verify_line_signature(
        body=body, signature=sig, channel_secret=secret,
    )
    assert ok is True
    assert detail == "ok"


def test_verify_signature_rejects_mismatch():
    """Tier 2: a signature computed with a different secret fails the
    constant-time compare with ``mismatch`` detail.
    """
    body = b'{"x":1}'
    bad_sig = _sign(body, "wrong-secret")
    ok, detail = verify_line_signature(
        body=body, signature=bad_sig, channel_secret="real-secret",
    )
    assert ok is False
    assert detail == "mismatch"


def test_verify_signature_rejects_missing_header():
    """Tier 2: no ``X-Line-Signature`` header → ``missing-signature``."""
    ok, detail = verify_line_signature(
        body=b"", signature="", channel_secret="s",
    )
    assert ok is False
    assert detail == "missing-signature"


# ── mint_envelope_from_line_event ─────────────────────────────────────


def test_mint_envelope_user_message():
    """Tier 2: a ``message`` event from a 1:1 user chat mints an
    envelope with ``sender="line:user:<userId>"`` and ExternalRef
    reply_to carrying the replyToken + source_id.
    """
    event = {
        "type": "message",
        "replyToken": "TOK_123",
        "source": {"type": "user", "userId": "U456"},
        "message": {"type": "text", "text": "hello bot"},
    }
    env = mint_envelope_from_line_event(event)
    assert env is not None
    assert env["text"] == "hello bot"
    assert env["sender"] == "line:user:U456"
    rt = env["reply_to"]
    assert isinstance(rt, ExternalRef)
    assert rt.transport == "line"
    assert rt.destination["reply_token"] == "TOK_123"
    assert rt.destination["source_type"] == "user"
    assert rt.destination["source_id"] == "U456"


def test_mint_envelope_group_message():
    """Tier 2: group source has both groupId + userId. Sender
    encodes both so the agent knows who said what in which group.
    """
    event = {
        "type": "message",
        "replyToken": "TOK",
        "source": {"type": "group", "groupId": "G999", "userId": "U456"},
        "message": {"type": "text", "text": "hi everyone"},
    }
    env = mint_envelope_from_line_event(event)
    assert env is not None
    assert env["sender"] == "line:group:G999:U456"
    assert env["reply_to"].destination["source_id"] == "G999"


def test_mint_envelope_room_message():
    """Tier 2: room source variant (= LINE multi-user chat without
    a group)."""
    event = {
        "type": "message",
        "replyToken": "TOK",
        "source": {"type": "room", "roomId": "R777", "userId": "U456"},
        "message": {"type": "text", "text": "hello room"},
    }
    env = mint_envelope_from_line_event(event)
    assert env is not None
    assert env["sender"] == "line:room:R777:U456"


def test_mint_envelope_skips_non_text_message():
    """Tier 2: sticker / image / location messages don't dispatch
    as plain text turns. Future multimodal envelope shape could
    surface them; this sample doesn't.
    """
    for msg_type in ("sticker", "image", "location", "video", "audio"):
        event = {
            "type": "message",
            "replyToken": "TOK",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": msg_type},
        }
        assert mint_envelope_from_line_event(event) is None


def test_mint_envelope_skips_non_message_events():
    """Tier 2: follow / unfollow / postback / etc. don't dispatch."""
    for event_type in ("follow", "unfollow", "postback", "join", "leave"):
        event = {
            "type": event_type,
            "source": {"type": "user", "userId": "U1"},
        }
        assert mint_envelope_from_line_event(event) is None


def test_mint_envelope_skips_empty_text():
    """Tier 2: a message with whitespace-only text doesn't dispatch."""
    event = {
        "type": "message",
        "replyToken": "TOK",
        "source": {"type": "user", "userId": "U1"},
        "message": {"type": "text", "text": "   "},
    }
    assert mint_envelope_from_line_event(event) is None


def test_mint_envelope_handles_missing_reply_token():
    """Tier 2: an event without ``replyToken`` (= unusual but
    possible for some event types) still mints an envelope with an
    empty reply_token string. Outbound dispatcher fall back to push
    API based on source_id.
    """
    event = {
        "type": "message",
        "source": {"type": "user", "userId": "U1"},
        "message": {"type": "text", "text": "hi"},
    }
    env = mint_envelope_from_line_event(event)
    assert env is not None
    assert env["reply_to"].destination["reply_token"] == ""


# ── register_router entry point ────────────────────────────────────────


def test_register_router_returns_none_when_target_agent_missing(monkeypatch):
    """Tier 2: register_router returns None if config has no
    ``target_agent`` — loader warns + skips mount.
    """
    from reyn.plugins.sample_line import register_router
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "x")
    assert register_router({}) is None
    assert register_router({"target_agent": ""}) is None


def test_register_router_returns_none_when_channel_secret_missing(monkeypatch):
    """Tier 2: register_router returns None if
    ``LINE_CHANNEL_SECRET`` env var is unset.
    """
    from reyn.plugins.sample_line import register_router
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    assert register_router({"target_agent": "x"}) is None


def test_register_router_returns_apirouter_when_configured(monkeypatch):
    """Tier 2: with both target_agent + channel secret, returns
    an APIRouter (= loader mounts it).
    """
    from fastapi import APIRouter

    from reyn.plugins.sample_line import register_router
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "x")
    router = register_router({"target_agent": "line_agent"})
    assert isinstance(router, APIRouter)


# ── route end-to-end via TestClient ───────────────────────────────────


@pytest.fixture()
def _line_client(monkeypatch):
    """FastAPI TestClient with a stubbed registry.

    Mounts the LINE plugin's router on a fresh app so tests stay
    hermetic (= no dependency on reyn.web.server module state).
    """
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

    from reyn.web import deps
    monkeypatch.setattr(deps, "_get_registry", _stub_get_registry)
    monkeypatch.setattr(deps, "_registry", None, raising=False)

    app = FastAPI()
    router = build_router(target_agent="line_agent")
    app.include_router(router)
    app.dependency_overrides[deps.get_registry] = _stub_get_registry

    client = TestClient(app)
    client.pushes = pushes  # type: ignore[attr-defined]
    yield client


def test_route_dispatches_signed_event_to_inbox(monkeypatch, _line_client):
    """Tier 2 end-to-end: a properly-signed LINE message event reaches
    the target agent's inbox with the right envelope.
    """
    secret = "channel-secret"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)

    body = json.dumps({
        "destination": "U-bot",
        "events": [{
            "type": "message",
            "replyToken": "TOK_xyz",
            "source": {"type": "user", "userId": "U456"},
            "message": {"type": "text", "text": "hello LINE bot"},
        }],
    }).encode()
    sig = _sign(body, secret)

    response = _line_client.post(
        "/webhook/line",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dispatched"] == 1

    pushes = _line_client.pushes
    assert len(pushes) == 1
    kind, payload = pushes[0]
    assert kind == "user"
    assert payload["text"] == "hello LINE bot"
    assert payload["sender"] == "line:user:U456"
    rt = payload["reply_to"]
    assert isinstance(rt, ExternalRef)
    assert rt.transport == "line"
    assert rt.destination["reply_token"] == "TOK_xyz"


def test_route_rejects_bad_signature(monkeypatch, _line_client):
    """Tier 2: wrong signature returns 401, no inbox push."""
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "real-secret")
    body = json.dumps({"events": []}).encode()
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
    assert len(_line_client.pushes) == 0


def test_route_rejects_missing_channel_secret(monkeypatch, _line_client):
    """Tier 2: ``LINE_CHANNEL_SECRET`` env var unset → 503."""
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    response = _line_client.post(
        "/webhook/line",
        content=b'{"events": []}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 503


def test_route_acks_empty_events_array(monkeypatch, _line_client):
    """Tier 2: a verify ping with empty events array returns 200
    ``ignored`` without pushing to inbox.
    """
    secret = "channel-secret"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    body = json.dumps({"events": []}).encode()
    sig = _sign(body, secret)

    response = _line_client.post(
        "/webhook/line",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )
    # Empty events array → 200 with dispatched=0.
    assert response.status_code == 200
    assert response.json()["dispatched"] == 0
    assert len(_line_client.pushes) == 0


def test_route_dispatches_multiple_events_in_one_post(monkeypatch, _line_client):
    """Tier 2: LINE bundles multiple events in a single webhook POST
    when they fire close together; the handler iterates and pushes
    each dispatchable one.
    """
    secret = "channel-secret"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)

    body = json.dumps({
        "events": [
            {
                "type": "message",
                "replyToken": "T1",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "first"},
            },
            {
                "type": "follow",   # skipped (= non-message)
                "source": {"type": "user", "userId": "U1"},
            },
            {
                "type": "message",
                "replyToken": "T2",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "second"},
            },
        ],
    }).encode()
    sig = _sign(body, secret)

    response = _line_client.post(
        "/webhook/line",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Line-Signature": sig,
        },
    )
    assert response.status_code == 200
    assert response.json()["dispatched"] == 2
    pushes = _line_client.pushes
    assert len(pushes) == 2
    assert pushes[0][1]["text"] == "first"
    assert pushes[1][1]["text"] == "second"
