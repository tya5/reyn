"""LINE Messaging API webhook — sample_line plugin (FP-0041 #489 plugins-api PR-2).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️ — see ``README.md``.

Inbound chat-transport for the LINE Messaging API, wired through
``line-bot-sdk`` v3 (= LINE's official SDK). Mirror of the
``sample_slack`` refactor: OSS SDK handles transport-specific
protocol; ``reyn.plugins.api`` handles Reyn-side dispatch. The
plugin's own glue is intentionally minimal (= ~50 lines).

## LINE Messaging API integration

LINE Developers Console (= https://developers.line.biz/console/):

  Webhook URL:           https://<reyn>/webhook/line
  Use webhook:           enabled
  Channel Secret:        set as ``LINE_CHANNEL_SECRET`` env var
  Channel Access Token:  set as ``LINE_CHANNEL_ACCESS_TOKEN`` env
                         var (= for outbound replies via LINE MCP)

## What line-bot-sdk handles (= we don't)

- HMAC-SHA256 + base64 signature verification (= ``WebhookParser``)
- Webhook envelope parsing into typed event objects
- Event-type discrimination (= MessageEvent / FollowEvent / etc.)
- Message-content discrimination (= TextMessageContent / etc.)
- Source-type variants (= UserSource / GroupSource / RoomSource)

## What we glue

- Map ``MessageEvent`` + ``TextMessageContent`` → Reyn envelope
- Use ``make_sender`` for the LINE attribution format
- Use ``push_to_agent`` for inbox dispatch
- Forward ``reply_token`` + source identifiers in ``ExternalRef``

## Reference

- line-bot-sdk:        https://github.com/line/line-bot-sdk-python
- Webhook spec:        https://developers.line.biz/en/reference/messaging-api/#webhooks
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)


def build_router(*, target_agent: str) -> APIRouter:
    """Build the LINE webhook router for the configured target agent.

    Wires ``line-bot-sdk``'s ``WebhookParser`` for signature
    verification + typed event parsing; routes message events to
    Reyn's agent inbox via ``reyn.plugins.api.push_to_agent``.

    Raises ``ImportError`` if ``line-bot-sdk`` isn't installed;
    ``register_router`` handles this by returning ``None``.
    """
    from linebot.v3 import WebhookParser
    from linebot.v3.exceptions import InvalidSignatureError
    from linebot.v3.webhooks import (
        GroupSource,
        MessageEvent,
        RoomSource,
        TextMessageContent,
        UserSource,
    )

    from reyn.plugins.api import make_sender, push_to_agent
    from reyn.runtime.transport import ExternalRef

    channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
    parser = WebhookParser(channel_secret)

    router = APIRouter(tags=["plugin-sample_line"])

    @router.post("/webhook/line")
    async def line_webhook(req: Request):
        body = await req.body()
        signature = req.headers.get("X-Line-Signature", "")
        try:
            events = parser.parse(body.decode("utf-8"), signature)
        except InvalidSignatureError as exc:
            logger.warning("LINE webhook signature verification failed: %s", exc)
            raise HTTPException(status_code=401, detail="invalid signature") from exc

        dispatched = 0
        for event in events:
            if not isinstance(event, MessageEvent):
                continue
            message = event.message
            if not isinstance(message, TextMessageContent):
                # Sticker / image / location etc. — not dispatched as
                # plain text turns in this sample.
                continue
            text = message.text or ""
            if not text.strip():
                continue

            source = event.source
            if isinstance(source, UserSource):
                user_id = source.user_id or "unknown"
                sender = make_sender("line", user_id, source_scope="user")
                source_id = user_id
                source_type = "user"
            elif isinstance(source, GroupSource):
                group_id = source.group_id or "unknown"
                user_id = source.user_id or ""
                sender = make_sender(
                    "line", group_id, source_scope="group", display=user_id,
                )
                source_id = group_id
                source_type = "group"
            elif isinstance(source, RoomSource):
                room_id = source.room_id or "unknown"
                user_id = source.user_id or ""
                sender = make_sender(
                    "line", room_id, source_scope="room", display=user_id,
                )
                source_id = room_id
                source_type = "room"
            else:
                continue

            reply_to = ExternalRef(
                transport="line",
                destination={
                    "reply_token": event.reply_token or "",
                    "source_type": source_type,
                    "source_id": source_id,
                },
            )
            try:
                await push_to_agent(
                    target_agent=target_agent,
                    text=text,
                    sender=sender,
                    reply_to=reply_to,
                )
                dispatched += 1
            except FileNotFoundError:
                logger.warning(
                    "sample_line: target agent %r not in registry; skipping",
                    target_agent,
                )
            except Exception as exc:
                logger.exception("sample_line: inbox push failed: %s", exc)

        return {"status": "ok", "dispatched": dispatched}

    return router
