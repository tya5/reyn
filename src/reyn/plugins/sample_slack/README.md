# sample_slack — Sample Slack Webhook Plugin

> ⚠️ **SAMPLE / EXAMPLE ONLY** ⚠️
>
> Reyn maintainers do **NOT** commit to keeping this code working
> against Slack API drift. This sample exists to demonstrate the
> Reyn plugin framework — production use requires your own fork or
> a community-maintained Slack plugin.

## What this is

A reference implementation of an inbound Slack webhook handler
showing how to:

- Register a Reyn webhook plugin via entry points
- Verify Slack's HMAC-SHA256 request signing
- Translate a Slack Events API payload into a Reyn inbox envelope
- Hand off to a Reyn agent's inbox

The transport-agnostic primitives (= ``ExternalRef`` reply_to,
outbox interceptor, MCP dispatcher) live in ``src/reyn/chat/`` and
``src/reyn/interfaces/web/`` and are reused by any future webhook plugin.

## Operator setup

1. Install Reyn with the ``sample_slack`` extra:
   ```bash
   pip install 'reyn[sample_slack]'   # = reyn + slack-bolt
   ```
2. Create a Slack App at https://api.slack.com/apps
3. Enable **Event Subscriptions** and set Request URL to
   ``https://<your-reyn-host>/webhook/slack``
4. Subscribe to bot events: ``app_mention`` and ``message.im``
5. Install the App in your workspace
6. Note the **Signing Secret** from the App credentials
7. Activate the plugin in ``webhooks.yaml`` (= next to reyn.yaml):
   ```yaml
   sample_slack:
     target_agent: news_agent     # agent that receives Slack messages
   ```
8. Set env vars on Reyn:
   ```bash
   export SLACK_SIGNING_SECRET=<signing-secret>
   export SLACK_BOT_TOKEN=xoxb-...    # for outbound replies via Slack MCP
   ```
9. At-mention the bot in any channel where it's invited; messages
   reach the target agent's inbox.

## Configuration (= webhooks.yaml)

| Key | Owner | Notes |
|-----|-------|-------|
| ``package`` | reyn-reserved | Optional. Disambiguates when multiple installed packages register ``sample_slack``. |
| ``enabled`` | reyn-reserved | Optional, default ``true``. Set ``false`` to deactivate without removing config. |
| ``target_agent`` | plugin | **Required**. Name of the Reyn agent that receives incoming Slack messages. |

Secrets (= ``SLACK_SIGNING_SECRET``, ``SLACK_BOT_TOKEN``) are
**always** environment variables, never in yaml.

## Production caveats

This sample intentionally omits production hardening:

- No per-user / per-channel ACL (= any Slack user in any subscribed
  channel can reach the target agent)
- No rate-limiting beyond what Slack itself enforces
- No retry logic beyond Slack's default
- Minimal error reporting (= log + 5xx for transient, 4xx for fatal)
- No bot identity verification beyond the signing secret
- No audit trail beyond Reyn's own events log

For production use:

- **Fork this plugin** into your own repo and harden as needed, or
- **Use a community-maintained external plugin** (= if one exists
  in your ecosystem), or
- **Write your own** using the framework spec at
  ``docs/plugins/authoring-guide.md``.

## Module structure

- ``__init__.py`` — entry-point target (= ``register_router``)
- ``webhook.py`` — Slack-specific protocol: signing verify, event
  parse, envelope mint, route handler factory

## License / status

Same license as Reyn proper, but **sample-tier maintenance**: bug
reports welcome, no SLA, breaking changes possible without notice.
