"""Slack Events API webhook — sample_slack plugin (FP-0041 #489 plugins-api PR-2).

⚠️  **SAMPLE / EXAMPLE ONLY** ⚠️ — see ``README.md``.

Inbound chat-transport for the Slack Events API, wired through
``slack-bolt`` (= Slack's official SDK). This refactor demonstrates
the canonical pattern for a Reyn webhook plugin:

  OSS SDK (= slack-bolt)  for transport-specific protocol
  reyn.gateway.api        for Reyn-side envelope dispatch

The plugin's own glue is intentionally minimal (= ~50 lines) so
plugin authors see exactly what "API-to-API" looks like.

## Slack Events API integration

Reyn's Slack App is configured at api.slack.com:

  Request URL:    https://<reyn>/webhook/slack
  Bot Events:     ``app_mention`` (= bot is @mentioned),
                  ``message.im`` (= DM to bot)
  Signing Secret: set as ``SLACK_SIGNING_SECRET`` env var on Reyn

## What slack-bolt handles (= we don't)

- HMAC-SHA256 v0 signature verification + replay window
- URL verification handshake challenge / response
- Event subscription dispatch (= ``@app.event(...)`` decorators)
- Retry deduplication via ``X-Slack-Retry-Num``
- OAuth flows (= if Reyn adds multi-workspace later)
- Socket Mode (= future option for hosted Reyn)

## What we glue (= the ~30 lines)

- Map ``app_mention`` / ``message`` event → Reyn envelope
- Use ``reyn.gateway.api.make_sender`` for the attribution string
- Use ``reyn.gateway.api.push_to_agent`` for inbox dispatch

## Reference

- slack-bolt-python:    https://slack.dev/bolt-python/
- FastAPI adapter:      ``slack_bolt.adapter.fastapi.async_handler``
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from reyn.mcp.extra_tool import ExtraTool

logger = logging.getLogger(__name__)


def build_send_tool() -> "ExtraTool":
    """Build the Slack outbound MCP tool (in-process send, #1805).

    The tool the agent invokes — via ``external_transports`` → ``route_to_mcp``
    → reyn-web's in-process MCP server — to deliver a reply to Slack. It calls
    ``chat.postMessage`` through ``slack_sdk``'s async client **in this reyn-web
    process**, so a complete plugin needs no separate Slack MCP server. A failed
    send returns a surfaced ``{"ok": false, "error": ...}`` result rather than a
    silent drop — the crash-vanish fix #1805 is about.
    """
    from reyn.mcp.extra_tool import ExtraTool

    async def _handler(args: dict) -> str:
        import json

        channel = (args or {}).get("channel")
        text = (args or {}).get("text", "")
        thread_ts = (args or {}).get("thread_ts")
        if not channel:
            return json.dumps({"ok": False, "error": "channel is required"})
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            return json.dumps({"ok": False, "error": "SLACK_BOT_TOKEN not set"})
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            return json.dumps(
                {"ok": False, "error": f"slack_sdk not installed: {exc}"},
            )
        client = AsyncWebClient(token=token)
        try:
            resp = await client.chat_postMessage(
                channel=channel, text=text, thread_ts=thread_ts,
            )
            return json.dumps({"ok": True, "ts": resp.get("ts")})
        except Exception as exc:  # noqa: BLE001 — surface the send failure
            return json.dumps({"ok": False, "error": str(exc)})

    return ExtraTool(
        name="slack_send",
        description=(
            "Send a message to a Slack channel or thread (outbound reply "
            "delivery for the sample_slack gateway plugin)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Slack channel ID."},
                "text": {"type": "string", "description": "Message text to post."},
                "thread_ts": {
                    "type": "string",
                    "description": "Optional thread ts to reply in-thread.",
                },
            },
            "required": ["channel", "text"],
            "additionalProperties": False,
        },
        handler=_handler,
    )


def build_router(*, target_agent: str) -> APIRouter:
    """Build the Slack webhook router for the configured target agent.

    Wires ``slack-bolt``'s ``AsyncApp`` to a FastAPI router via the
    ``AsyncSlackRequestHandler``. The bot's incoming events (=
    ``app_mention`` + ``message``) are dispatched to Reyn's agent
    inbox via ``reyn.gateway.api.push_to_agent``.

    Raises ``RuntimeError`` if ``slack-bolt`` isn't installed; the
    ``register_router`` entry point handles this by returning ``None``
    so the loader logs + skips.
    """
    # SDK imports kept inside ``build_router`` so the module is
    # importable without the SDK (= ``register_router`` decides the
    # opt-out behaviour).
    from slack_bolt.adapter.fastapi.async_handler import (
        AsyncSlackRequestHandler,
    )
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.authorization import AuthorizeResult

    from reyn.gateway.api import make_sender, push_to_agent
    from reyn.runtime.transport import ExternalRef

    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    # Reyn dispatches outbound replies via the Slack MCP server, NOT
    # through bolt. Bolt's default ``single_team_authorization`` calls
    # ``auth.test`` against Slack to verify the bot token at request
    # time — we skip that probe with a custom ``authorize`` that
    # returns a stub AuthorizeResult so inbound dispatch works without
    # a real bot token configured.
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or "xoxb-placeholder"

    async def _authorize(*args, **kwargs):
        return AuthorizeResult(
            enterprise_id=None,
            team_id=None,
            user_id=None,
            bot_user_id=None,
            bot_id=None,
            bot_token=bot_token,
        )

    bolt_app = AsyncApp(
        signing_secret=signing_secret,
        authorize=_authorize,
    )

    async def _dispatch(event: dict) -> None:
        """Translate a Slack message-class event → Reyn envelope +
        push via the stable plugin API.
        """
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return  # bot echo, drop
        user_id = event.get("user") or "unknown"
        channel = event.get("channel")
        if not isinstance(channel, str):
            return
        ts = event.get("ts")
        thread_ts = event.get("thread_ts") or ts

        try:
            await push_to_agent(
                target_agent=target_agent,
                text=text,
                sender=make_sender("slack", user_id),
                reply_to=ExternalRef(
                    transport="slack",
                    destination={"channel": channel, "thread_ts": thread_ts},
                ),
            )
        except FileNotFoundError:
            logger.warning(
                "sample_slack: target agent %r not in registry; skipping",
                target_agent,
            )
        except Exception as exc:
            logger.exception("sample_slack: inbox push failed: %s", exc)

    @bolt_app.event("app_mention")
    async def on_app_mention(event):  # noqa: D401
        await _dispatch(event)

    @bolt_app.event("message")
    async def on_message(event):  # noqa: D401
        await _dispatch(event)

    handler = AsyncSlackRequestHandler(bolt_app)
    router = APIRouter(tags=["plugin-sample_slack"])

    @router.post("/webhook/slack")
    async def slack_webhook(req: Request):
        """Forward incoming Slack POSTs to bolt's handler.

        Bolt internally verifies signing, parses events, and routes
        to the registered handlers (= ``on_app_mention`` /
        ``on_message`` above). URL verification handshake +
        retry dedup are bolt's responsibility too.
        """
        return await handler.handle(req)

    return router
