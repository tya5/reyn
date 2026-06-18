"""Tier 2: sample_slack plugin (FP-0041 plugins-api PR-2).

The hand-rolled webhook handler that landed in PR #522 has been
replaced with ``slack-bolt`` (= Slack's official SDK). Bolt handles
signing / event parsing / URL verification; the plugin just glues
bolt events → Reyn ``push_to_agent``.

These tests are deliberately thin compared to the pre-refactor
suite (= old tests pinned hand-rolled signing + envelope mint
which are now bolt's responsibility). What we test now:

  1. ``register_router`` entry-point contract (= missing config /
     env / SDK → None opt-out).
  2. ``build_router`` actually mounts a ``/webhook/slack`` route.
  3. Round-trip integration via TestClient + a signed bolt-shaped
     payload + stubbed registry: agent receives the right envelope
     (= sender via ``make_sender``, ``ExternalRef`` reply_to with
     channel + thread_ts).
  4. Non-text / bot-echo events skip dispatch (= filter logic in
     our ``_dispatch`` closure).

Tier 2 because the sample is the operator-facing reference for
Slack chat-transport — a regression silently breaks the integration.

When ``slack-bolt`` isn't installed, the SDK-using tests skip via
``pytest.importorskip``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

# Skip the SDK-dependent tests when slack-bolt isn't installed (= when
# reyn is installed without the ``sample_slack`` extra).
slack_bolt = pytest.importorskip("slack_bolt")


def _sign(body: bytes, secret: str, ts: str) -> str:
    """Build the Slack signature header value for body + timestamp."""
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


# ── register_router entry point ────────────────────────────────────────


def test_register_router_returns_none_when_target_agent_missing(monkeypatch):
    """Tier 2: register_router returns None if config has no
    ``target_agent`` — loader warns + skips mount.
    """
    from reyn.plugins.sample_slack import register_router
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")
    assert register_router({}) is None
    assert register_router({"target_agent": ""}) is None


def test_register_router_returns_none_when_signing_secret_missing(monkeypatch):
    """Tier 2: register_router returns None if SLACK_SIGNING_SECRET
    env var is unset.
    """
    from reyn.plugins.sample_slack import register_router
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    assert register_router({"target_agent": "x"}) is None


def test_register_router_returns_apirouter_when_configured(monkeypatch):
    """Tier 2: with both target_agent + signing secret, returns an
    APIRouter (= loader mounts it).
    """
    from fastapi import APIRouter

    from reyn.plugins.sample_slack import register_router
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")
    router = register_router({"target_agent": "news_agent"})
    assert isinstance(router, APIRouter)


# ── route mounted on a FastAPI app ────────────────────────────────────


def test_build_router_mounts_webhook_slack_path(monkeypatch):
    """Tier 2: ``build_router`` produces a router with ``/webhook/slack``
    registered as POST. Without this, the operator's Slack App
    Request URL hits 404.
    """
    from fastapi import FastAPI

    from reyn.plugins.sample_slack.webhook import build_router
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")

    app = FastAPI()
    app.include_router(build_router(target_agent="news_agent"))
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/webhook/slack" in paths


# ── end-to-end: signed Slack event → Reyn agent inbox ─────────────────


@pytest.fixture()
def _slack_client(monkeypatch):
    """FastAPI TestClient with a stubbed AgentRegistry.

    Patches the reyn.plugins.api module so push_to_agent dispatches
    to the stub registry; captures pushed envelopes for assertions.
    """
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

        # FP-0043 S4b-5: deliver_to_agent routes to a per-sender webhook session via
        # resolve_session + ensure_session_running (every _StubSession shares the
        # ``pushed`` capture list, so the routing detail is transparent here).
        def resolve_session(self, name, transport, native_id):
            return _StubSession()

        def ensure_session_running(self, name, sid):
            return None

        def list_names(self):
            return ["news_agent"]

        def exists(self, name):
            return name == "news_agent"

    from reyn.interfaces.web import deps
    monkeypatch.setattr(deps, "_get_registry", lambda: _StubRegistry())
    monkeypatch.setattr(deps, "_registry", None, raising=False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    from reyn.plugins.sample_slack.webhook import build_router
    app = FastAPI()
    app.include_router(build_router(target_agent="news_agent"))

    client = TestClient(app)
    client.pushed = pushed  # type: ignore[attr-defined]
    yield client


def _post_signed_event(client, secret: str, event_payload: dict):
    body = json.dumps({"event": event_payload, "type": "event_callback"}).encode()
    ts = str(int(time.time()))
    sig = _sign(body, secret, ts)
    return client.post(
        "/webhook/slack",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )


def test_app_mention_dispatches_to_agent_inbox(_slack_client):
    """Tier 2: an ``app_mention`` event signed correctly
    reaches the target agent's inbox with the right envelope.
    """
    response = _post_signed_event(_slack_client, "test-secret", {
        "type": "app_mention",
        "user": "U456",
        "text": "<@U_BOT> hello",
        "channel": "C123",
        "ts": "1234.5678",
    })
    assert response.status_code == 200
    pushed = _slack_client.pushed
    assert pushed, "expected at least one push call to the agent"
    kind, payload = pushed[0]
    assert kind == "user"
    assert payload["text"] == "<@U_BOT> hello"
    assert payload["sender"] == "slack:U456"

    from reyn.runtime.transport import ExternalRef
    assert isinstance(payload["reply_to"], ExternalRef)
    assert payload["reply_to"].transport == "slack"
    assert payload["reply_to"].destination == {
        "channel": "C123",
        "thread_ts": "1234.5678",
    }


def test_message_event_with_thread_preserves_thread_ts(_slack_client):
    """Tier 2: when the user replies in an existing thread,
    ``reply_to.thread_ts`` is the thread's parent ts (= not the
    message's own ts) so agent replies join the right thread.
    """
    response = _post_signed_event(_slack_client, "test-secret", {
        "type": "message",
        "user": "U456",
        "text": "follow-up",
        "channel": "C123",
        "ts": "9999.0001",
        "thread_ts": "1234.5678",
    })
    assert response.status_code == 200
    _, payload = _slack_client.pushed[0]
    assert payload["reply_to"].destination["thread_ts"] == "1234.5678"


def test_bot_echo_message_is_not_dispatched(_slack_client):
    """Tier 2: a message with ``subtype=bot_message`` (= our own bot
    posting) is filtered out to prevent feedback loops.
    """
    _post_signed_event(_slack_client, "test-secret", {
        "type": "message",
        "subtype": "bot_message",
        "bot_id": "B999",
        "text": "I (bot) just posted",
        "channel": "C123",
        "ts": "1.0",
    })
    assert _slack_client.pushed == []


def test_empty_text_message_is_not_dispatched(_slack_client):
    """Tier 2: whitespace-only text doesn't dispatch (= no value
    forwarding to the LLM, no envelope spam).
    """
    _post_signed_event(_slack_client, "test-secret", {
        "type": "message",
        "user": "U1",
        "text": "   ",
        "channel": "C1",
        "ts": "1.0",
    })
    assert _slack_client.pushed == []
