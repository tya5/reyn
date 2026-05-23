"""LINE Messaging API webhook — sample_line plugin (FP-0041 #489 PR-E).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️ — see ``README.md``.

Inbound chat-transport for the LINE Messaging API. Receives webhook
POSTs from LINE's platform, verifies the ``X-Line-Signature`` HMAC,
parses the events array, mints a Reyn inbox envelope per dispatchable
message event, and pushes to the configured target agent.

## LINE specifics that differ from Slack

- **Signing**: HMAC-SHA256 of the raw body, **base64-encoded**
  (= Slack uses hex). Header is ``X-Line-Signature``.
- **No replay window**: LINE doesn't include a timestamp in the
  signing input, so there's nothing to compare against the clock.
  Replay attacks are mitigated by the ``replyToken``'s single-use
  semantics on the LINE side (= only useful within ~30 seconds of
  the original event).
- **Events array**: payload is ``{"events": [...]}`` with a list
  of events, not a single event under ``event:``. The handler
  iterates and dispatches each ``message`` event.
- **Source variants**: an event's source is one of
  ``user`` / ``group`` / ``room``; sender attribution + reply
  destination differ per source type.
- **replyToken**: each event carries a single-use, time-limited
  token for the LINE Reply API. The outbound side (= via Reyn's
  external_transports + LINE MCP server) uses it; this inbound
  handler simply forwards it in the ``ExternalRef.destination``.

## Reference

- LINE Messaging API webhook spec:
  https://developers.line.biz/en/reference/messaging-api/#webhooks
- Signature verification:
  https://developers.line.biz/en/reference/messaging-api/#signature-validation
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from reyn.chat.transport import ExternalRef
from reyn.web.deps import get_registry

logger = logging.getLogger(__name__)


# ── signing verification ──────────────────────────────────────────────


def verify_line_signature(
    *, body: bytes, signature: str, channel_secret: str,
) -> tuple[bool, str]:
    """Verify a LINE-signed webhook request.

    Returns ``(ok, detail)``. ``ok=True`` means the signature
    matches; ``detail`` is a short reason for ``ok=False`` cases
    (= ``missing-signature`` / ``mismatch``). Constant-time
    comparison via ``hmac.compare_digest``.

    LINE's spec: ``signature = base64(HMAC-SHA256(channel_secret, body))``.
    No timestamp involved — replay is mitigated by ``replyToken``
    single-use semantics on the LINE side.
    """
    if not signature:
        return False, "missing-signature"
    digest = hmac.new(
        channel_secret.encode(), body, hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, signature):
        return False, "mismatch"
    return True, "ok"


# ── envelope minting ─────────────────────────────────────────────────


def mint_envelope_from_line_event(event: dict) -> dict | None:
    """Convert a single LINE webhook event into a Reyn inbox envelope.

    Returns ``None`` when the event isn't a dispatchable message
    (= follow / unfollow / postback / sticker without text / etc.).

    Sender shape:
      ``line:user:<userId>``   — 1:1 chat
      ``line:group:<groupId>:<userId>``   — group chat
      ``line:room:<roomId>:<userId>``     — room chat

    Reply-to shape:
      ExternalRef(transport="line", destination={
          "reply_token": <replyToken>,   # single-use, 30-sec window
          "source_type": "user" | "group" | "room",
          "source_id": <userId | groupId | roomId>,
      })

    The dispatcher (= LINE MCP server side) chooses between the
    Reply API (= using reply_token) and the Push API (= using
    source_id) based on timing.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "message":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("type") != "text":
        # Sticker / image / location etc. — not directly dispatchable
        # as a plain text turn. Future plugin enhancements could
        # surface these to the LLM via multimodal envelope shape.
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    source = event.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "user":
        user_id = source.get("userId", "")
        sender = f"line:user:{user_id}" if user_id else "line:user:unknown"
        source_id = user_id
    elif source_type == "group":
        group_id = source.get("groupId", "")
        user_id = source.get("userId", "")
        sender = f"line:group:{group_id}:{user_id}" if group_id else "line:group:unknown"
        source_id = group_id
    elif source_type == "room":
        room_id = source.get("roomId", "")
        user_id = source.get("userId", "")
        sender = f"line:room:{room_id}:{user_id}" if room_id else "line:room:unknown"
        source_id = room_id
    else:
        return None

    reply_token = event.get("replyToken")
    if not isinstance(reply_token, str):
        reply_token = ""

    reply_to = ExternalRef(
        transport="line",
        destination={
            "reply_token": reply_token,
            "source_type": source_type,
            "source_id": source_id,
        },
    )
    return {"text": text, "sender": sender, "reply_to": reply_to}


# ── route factory ────────────────────────────────────────────────────


def build_router(*, target_agent: str) -> APIRouter:
    """Build the LINE webhook router for the configured target agent.

    Called by the plugin's ``register_router`` entry point with the
    resolved ``target_agent`` from ``webhooks.yaml``. Captures the
    agent in the closure so the route handler doesn't re-resolve
    per request.

    The channel secret is read from ``LINE_CHANNEL_SECRET`` env var
    at request time (= not at build time, so operator can rotate
    without restart — LINE-side rotation requires a brief overlap
    window anyway).
    """
    router = APIRouter(tags=["plugin-sample_line"])

    @router.post("/webhook/line")
    async def line_webhook(
        request: Request,
        registry=Depends(get_registry),
    ) -> Response:
        """Receive a LINE Messaging API POST.

        Behaviour:
          1. Verify ``X-Line-Signature``. Bad → 401.
          2. Parse body as JSON; iterate events array.
          3. For each dispatchable message event, mint envelope +
             push to target agent's inbox.
          4. Return 200 (= LINE retries 4xx/5xx within the same
             webhook delivery attempt budget; 2xx ends the
             attempt).
        """
        body_bytes = await request.body()

        channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
        if not channel_secret:
            logger.warning(
                "LINE webhook received but LINE_CHANNEL_SECRET is unset; rejecting.",
            )
            return JSONResponse(
                {"error": "LINE channel secret not configured"},
                status_code=503,
            )

        ok, detail = verify_line_signature(
            body=body_bytes,
            signature=request.headers.get("X-Line-Signature", ""),
            channel_secret=channel_secret,
        )
        if not ok:
            logger.warning("LINE webhook signing verify failed: %s", detail)
            return JSONResponse(
                {"error": f"signature verification failed: {detail}"},
                status_code=401,
            )

        # Parse body.
        try:
            import json
            payload = json.loads(body_bytes.decode("utf-8") or "{}")
        except Exception:
            logger.warning("LINE webhook: malformed JSON body")
            return JSONResponse(
                {"error": "malformed body"}, status_code=400,
            )

        events: Any = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            # No events array → ack (= e.g. LINE verify ping).
            return JSONResponse({"status": "ignored"}, status_code=200)

        # Push each dispatchable event via the stable plugin API
        # (= reyn.plugins.api, FP-0041 plugins-api).
        from reyn.plugins.api import push_to_agent

        dispatched = 0
        for event in events:
            envelope = mint_envelope_from_line_event(event)
            if envelope is None:
                continue
            try:
                await push_to_agent(
                    target_agent=target_agent,
                    text=envelope["text"],
                    sender=envelope["sender"],
                    reply_to=envelope["reply_to"],
                    registry=registry,
                )
                dispatched += 1
            except FileNotFoundError:
                logger.warning(
                    "LINE webhook: target agent %r not found in registry",
                    target_agent,
                )
                return JSONResponse(
                    {"error": f"target agent {target_agent!r} not found"},
                    status_code=503,
                )
            except Exception as exc:
                logger.exception("LINE webhook: inbox push failed: %s", exc)
                return JSONResponse(
                    {"error": f"inbox dispatch failed: {type(exc).__name__}"},
                    status_code=500,
                )

        return JSONResponse(
            {"status": "ok", "dispatched": dispatched}, status_code=200,
        )

    return router
