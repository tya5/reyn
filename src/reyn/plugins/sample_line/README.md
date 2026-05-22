# sample_line — Sample LINE Webhook Plugin

> ⚠️ **SAMPLE / EXAMPLE ONLY** ⚠️
>
> Reyn maintainers do **NOT** commit to keeping this code working
> against LINE Messaging API drift. This sample exists to demonstrate
> the Reyn plugin framework — production use requires your own fork
> or a community-maintained LINE plugin.

## What this is

A reference implementation of an inbound LINE Messaging API webhook
handler, mirror of ``sample_slack`` for LINE. Demonstrates:

- Registering a Reyn webhook plugin via entry points
- LINE's HMAC-SHA256 + base64 signature verification (= different
  from Slack's hex format)
- Translating a LINE event into a Reyn inbox envelope
- Forwarding the LINE ``replyToken`` so the outbound side (= via
  LINE MCP server) can use the Reply API

## Operator setup

1. Create a LINE Messaging API channel at
   https://developers.line.biz/console/
2. Note the **Channel Secret** + **Channel Access Token** from the
   channel's "Basic settings" + "Messaging API" tabs
3. Set the **Webhook URL** to ``https://<your-reyn-host>/webhook/line``
4. Enable **Use webhook** in the Messaging API settings
5. Activate the plugin in ``webhooks.yaml``:
   ```yaml
   sample_line:
     target_agent: line_agent     # agent that receives LINE messages
   ```
6. Set env vars on Reyn:
   ```bash
   export LINE_CHANNEL_SECRET=<channel-secret>
   export LINE_CHANNEL_ACCESS_TOKEN=<channel-access-token>   # for outbound replies
   ```
7. Send a message to your LINE bot; it reaches the target agent's inbox.

## Configuration (= webhooks.yaml)

| Key | Owner | Notes |
|-----|-------|-------|
| ``package`` | reyn-reserved | Optional. Disambiguates when multiple installed packages register ``sample_line``. |
| ``enabled`` | reyn-reserved | Optional, default ``true``. |
| ``target_agent`` | plugin | **Required**. Name of the Reyn agent that receives LINE messages. |

Secrets (= ``LINE_CHANNEL_SECRET`` and ``LINE_CHANNEL_ACCESS_TOKEN``)
are **always** environment variables, never in yaml.

## How LINE differs from Slack (= compared with sample_slack)

| Aspect | sample_slack | sample_line |
|--------|--------------|-------------|
| Signature header | ``X-Slack-Signature`` (v0=hex) | ``X-Line-Signature`` (base64) |
| Signing input | ``v0:<ts>:<body>`` | ``<body>`` (no timestamp) |
| Replay window | 5 minutes (= timestamp check) | None (= ``replyToken`` single-use) |
| URL verification | Initial ``url_verification`` challenge | None at the webhook level |
| Payload shape | Single event under ``event:`` | Events array under ``events:`` |
| Source variants | Channels + DMs | user / group / room |
| Reply target | ``channel`` + ``thread_ts`` | ``replyToken`` + source_id |

## Production caveats

Same as ``sample_slack``: no per-user ACL beyond webhook signing,
minimal error reporting, fork-and-harden for production. See the
``docs/plugins/authoring-guide.md`` for guidance on writing your own.

## Module structure

- ``__init__.py`` — entry-point target (= ``register_router``)
- ``webhook.py`` — LINE-specific protocol: signing verify, event
  parse, envelope mint, route handler factory
