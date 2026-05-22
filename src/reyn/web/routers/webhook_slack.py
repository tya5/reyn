"""Slack inbound webhook router — FP-0041 #489 PR-D.

Inbound chat-transport adapter: receives Slack Events API webhook
POSTs at ``/webhook/slack``, verifies the signing secret, parses
the event, mints an inbox envelope with ``sender="slack:<user_id>:
<display>"`` + ``reply_to=ExternalRef(transport="slack",
destination={...})``, and pushes to the target agent's inbox.

The agent's router_loop then processes the message as an attributed
turn (= PR-A dispatch attribution emits a ``[context shift]``
state_change before the LLM sees the text). Replies flow back out
via PR-D2 (= outbox subscriber + route_to_mcp via Slack MCP server).
This PR covers inbound only.

## Slack Events API integration

Reyn's Slack App is configured at api.slack.com → Event Subscriptions:
  Request URL: https://<reyn>/webhook/slack
  Subscribe to bot events: ``app_mention`` (= bot is @mentioned),
    ``message.im`` (= DM to bot)
  Signing Secret: set as ``SLACK_SIGNING_SECRET`` env var on Reyn

## Signing verification

Slack signs every request with HMAC-SHA256 using the signing secret.
Verification per https://api.slack.com/authentication/verifying-requests-from-slack:

  base_string = f"v0:{timestamp}:{body}"
  signature = "v0=" + hmac.sha256(secret, base_string).hexdigest()

  Reject when:
    - Request older than 5 minutes (= replay guard)
    - X-Slack-Signature mismatch (= constant-time compare)

## URL verification handshake

When Slack first calls the Request URL, it sends:
  {"type": "url_verification", "challenge": "<random>"}

Reyn echoes back ``{"challenge": "<random>"}`` (= 200 OK).

## Event shapes handled

  app_mention / message:
    {"event": {"type": "app_mention", "user": "U456",
               "text": "<@U_BOT> help", "channel": "C123",
               "ts": "1234.5678", "thread_ts": "..." | absent}}

Other event types (= reactions, channel join, etc.) are accepted
and acknowledged but produce no inbox envelope.

## Target agent routing

Phase 1 (= this PR): single target agent configured per Slack workspace
via ``SLACK_TARGET_AGENT`` env var (or ``slack.target_agent`` in
reyn.yaml). Per-user / per-channel ACL is follow-up.

## Defensive posture

- Signing failure → 401 + log warning
- URL verification → 200 + challenge echo
- Unknown event type → 200 (= Slack will keep delivering)
- Inbox push failure → 500 + log; Slack will retry
- Reyn target agent not found → 500 + log

The handler never crashes the FastAPI route; all error paths return
the appropriate HTTP status with no exception propagation.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from reyn.chat.transport import ExternalRef
from reyn.web.deps import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

# Replay-protection window for Slack signing. Per Slack docs, ts older
# than 5 minutes should be rejected to prevent replay attacks.
_SLACK_REPLAY_WINDOW_SECONDS = 60 * 5


# ── signing verification ──────────────────────────────────────────────


def verify_slack_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    now: float | None = None,
) -> tuple[bool, str]:
    """Verify a Slack-signed webhook request.

    Returns ``(ok, detail)``. ``ok=True`` means the signature
    matches AND the timestamp is within the replay window;
    ``detail`` is a short reason string for ``ok=False`` cases
    (= "missing-timestamp" / "missing-signature" / "stale" /
    "mismatch"). Constant-time comparison via ``hmac.compare_digest``.

    Exported for test injection — callers (= the route handler) use
    this with ``now=time.time()``.
    """
    if not timestamp:
        return False, "missing-timestamp"
    if not signature:
        return False, "missing-signature"
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "invalid-timestamp"
    current = now if now is not None else time.time()
    if abs(current - ts) > _SLACK_REPLAY_WINDOW_SECONDS:
        return False, "stale"
    base_string = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(
        signing_secret.encode(), base_string, hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature):
        return False, "mismatch"
    return True, "ok"


# ── envelope minting ─────────────────────────────────────────────────


def mint_envelope_from_slack_event(event: dict) -> dict | None:
    """Convert a Slack event payload into a Reyn inbox envelope.

    Returns the envelope dict (= ``{"text", "sender", "reply_to"}``)
    or ``None`` when the event isn't a kind we should dispatch (=
    bot's own message, reaction, channel-join, etc.).

    Sender shape: ``slack:<user_id>`` (= per PR-A sender
    convention). Display name lookup is not implemented in this PR
    (= optional augmentation in follow-up if Slack MCP exposes a
    ``users.info`` call).

    Reply-to shape: ``ExternalRef(transport="slack",
    destination={"channel": <id>, "thread_ts": <id>})`` — the
    ``thread_ts`` defaults to the message's own ``ts`` so the agent
    reply lands in a thread; if the user is replying in an existing
    thread, that ``thread_ts`` is preserved.

    Stored as a plain dict because the existing inbox mechanism uses
    dict payloads. The ``reply_to`` is the ExternalRef instance; PR-D2
    outbox subscriber will read it back.
    """
    inner = event.get("event") if isinstance(event, dict) else None
    if not isinstance(inner, dict):
        return None
    event_type = inner.get("type")
    # Only dispatch message-class events. App-home / reaction / etc.
    # land here but produce no inbox push.
    if event_type not in ("app_mention", "message"):
        return None
    # Ignore bot's own messages (= prevent feedback loops).
    if inner.get("bot_id") or inner.get("subtype") == "bot_message":
        return None
    text = inner.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    user_id = inner.get("user")
    channel = inner.get("channel")
    if not isinstance(channel, str):
        return None
    ts = inner.get("ts")
    thread_ts = inner.get("thread_ts") or ts
    sender = f"slack:{user_id}" if user_id else "slack:unknown"
    reply_to = ExternalRef(
        transport="slack",
        destination={"channel": channel, "thread_ts": thread_ts},
    )
    return {
        "text": text,
        "sender": sender,
        "reply_to": reply_to,
    }


# ── target agent resolution (= MVP single-agent shape) ────────────────


def _resolve_target_agent(config: Any) -> str | None:
    """Resolve the target agent for Slack inbound messages.

    MVP shape (= follow-up PR will add per-user/per-channel ACL):

      1. ``SLACK_TARGET_AGENT`` env var (= operator quick-config)
      2. ``slack.target_agent`` in reyn.yaml (= TBD wiring)
      3. Default ``"default"`` (= will fail if no such agent exists,
         and the failure shows up as 500 with a clear log line)
    """
    env = os.environ.get("SLACK_TARGET_AGENT")
    if env:
        return env
    # config-side wiring lands when ReynConfig.slack section ships.
    return None


# ── route ────────────────────────────────────────────────────────────


@router.post("/webhook/slack")
async def slack_webhook(
    request: Request,
    registry=Depends(get_registry),
) -> Response:
    """Receive a Slack Events API POST.

    Three branches:
      1. URL verification → echo challenge.
      2. Event with no dispatchable payload (= non-message events,
         malformed payload) → 200 ack only.
      3. Message event → verify signing, mint envelope, push to
         target agent's inbox, return 200.

    Errors return appropriate HTTP status without raising; Slack
    will retry on non-2xx so we avoid 4xx/5xx for transient cases.
    """
    body_bytes = await request.body()

    # Parse body up-front so we can handle url_verification before
    # signing check (= URL verification doesn't have a signature
    # during initial setup with some Slack App configurations).
    try:
        import json
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except Exception:
        logger.warning("Slack webhook: malformed JSON body")
        return JSONResponse(
            {"error": "malformed body"}, status_code=400,
        )

    # URL verification handshake — Slack POSTs this on initial setup.
    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        return JSONResponse(
            {"challenge": challenge if isinstance(challenge, str) else ""},
            status_code=200,
        )

    # Signing verification for all other paths.
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not signing_secret:
        logger.warning(
            "Slack webhook received but SLACK_SIGNING_SECRET is unset; rejecting.",
        )
        return JSONResponse(
            {"error": "Slack signing secret not configured"},
            status_code=503,
        )
    ok, detail = verify_slack_signature(
        body=body_bytes,
        timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
        signature=request.headers.get("X-Slack-Signature", ""),
        signing_secret=signing_secret,
    )
    if not ok:
        logger.warning("Slack webhook signing verify failed: %s", detail)
        return JSONResponse(
            {"error": f"signature verification failed: {detail}"},
            status_code=401,
        )

    # Mint envelope from the event.
    envelope = mint_envelope_from_slack_event(payload)
    if envelope is None:
        # Non-dispatchable event (= reaction / channel_join / bot
        # message echo / etc.). Slack expects 2xx so it won't retry.
        return JSONResponse({"status": "ignored"}, status_code=200)

    # Resolve target agent.
    target = _resolve_target_agent(None)
    if not target:
        logger.warning(
            "Slack webhook: no target agent configured "
            "(SLACK_TARGET_AGENT env var unset)",
        )
        return JSONResponse(
            {"error": "no target agent configured"},
            status_code=503,
        )

    # Push to inbox via the registry (= ensure_running so the agent's
    # router_loop is live to consume).
    try:
        session = await registry.ensure_running(target)
    except FileNotFoundError:
        logger.warning(
            "Slack webhook: target agent %r not found in registry", target,
        )
        return JSONResponse(
            {"error": f"target agent {target!r} not found"},
            status_code=503,
        )
    try:
        await session._put_inbox("user", dict(envelope))
    except Exception as exc:
        logger.exception("Slack webhook: inbox push failed: %s", exc)
        return JSONResponse(
            {"error": f"inbox dispatch failed: {type(exc).__name__}"},
            status_code=500,
        )

    return JSONResponse({"status": "ok"}, status_code=200)
