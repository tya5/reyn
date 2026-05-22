# Reyn plugin authoring guide

> FP-0041 #489 follow-up — plugin framework spec for Reyn integrations
> (= webhook handlers for Slack / LINE / Discord / GitHub etc.).

This guide is for **plugin authors** who want to add inbound webhook
integrations to Reyn (= e.g. a chat-transport adapter for a service
not covered by the bundled samples).

## Design rationale

Reyn core stays free of transport-specific protocol code: signing
schemes, event payload schemas, and SDK dependencies all live in
plugins. The trade-off — operator installs an extra package — is
worth it because:

- Reyn maintainers don't track Slack / LINE / Discord / etc. API drift
- Each transport's SDK choices stay local to its plugin
- Operators run only the plugins they need
- Community can ship plugins independent of Reyn's release cycle

The bundled ``sample_*`` plugins under ``src/reyn/plugins/`` exist as
reference implementations + quick-start fixtures. Production
integrations should fork or replace them.

## Plugin shape

A webhook plugin is a Python package that exposes a single callable
named ``register_router`` (= conventional) via the ``reyn.webhooks``
entry point group.

### Layout (= external package example)

```
my-reyn-webhook-line/
├── pyproject.toml
└── my_reyn_webhook_line/
    ├── __init__.py     # exposes register_router
    └── handler.py      # actual route logic
```

### Entry point declaration

```toml
# pyproject.toml
[project]
name = "my-reyn-webhook-line"
version = "0.1.0"
dependencies = ["fastapi", "reyn"]

[project.entry-points."reyn.webhooks"]
line = "my_reyn_webhook_line:register_router"
```

The entry point name (= ``line``) is what operators put in their
``webhooks.yaml`` to activate this plugin.

### register_router contract

```python
from fastapi import APIRouter

def register_router(config: dict) -> APIRouter | None:
    """Build the plugin's webhook router.

    config: per-instance dict from webhooks.yaml, minus reyn-reserved
            keys (package, enabled). Plugin-defined fields only.
    returns: an APIRouter to mount, or None to skip (= e.g. required
             option missing). When returning None, log a warning so
             the operator can see why.
    """
    ...
```

The returned ``APIRouter`` is mounted on the Reyn web app at the path
the plugin chooses. By convention webhook plugins use
``/webhook/<service>`` as the route path so operators paste a
predictable URL into their service's webhook config UI.

## webhooks.yaml schema

Operators activate plugins in ``webhooks.yaml`` (= sibling of
``reyn.yaml`` at the project root):

```yaml
# webhooks.yaml

# Short form: just the plugin name (= empty value)
sample_slack:
  target_agent: news_agent      # plugin-defined field

# Long form: explicit reyn-reserved fields + plugin fields
my_other_plugin:
  package: reyn-plugin-line     # optional: disambiguates same-name plugins
  enabled: false                # optional, default true
  some_option: value            # plugin-defined
```

### Reyn-reserved keys

The loader interprets these and removes them from what's passed to
``register_router``:

| Key | Default | Purpose |
|-----|---------|---------|
| ``package`` | unset | Disambiguates when multiple packages register the same plugin name. Match against the Python distribution name. |
| ``enabled`` | ``true`` | Set ``false`` to deactivate without removing config. |

Plugin authors must avoid using these names in their plugin-defined
fields.

### Per-plugin options

Everything in the per-plugin dict except the reyn-reserved keys is
forwarded to ``register_router`` as the ``config`` argument. The
plugin author defines this schema.

Secrets (= API keys, signing secrets) belong in **environment
variables**, never in webhooks.yaml.

## Inbound envelope shape

When the plugin's route receives a webhook, it should mint an
envelope and push to the target agent's inbox:

```python
from reyn.chat.transport import ExternalRef

envelope = {
    "text": "<message body>",
    "sender": f"<transport>:<external_user_id>",
    "reply_to": ExternalRef(
        transport="<transport>",        # e.g. "slack", "line"
        destination={                   # transport-specific routing
            "channel": "...",
            "thread_ts": "...",
        },
    ),
}

# Push via the AgentRegistry (= from reyn.web.deps):
session = await registry.ensure_running(target_agent)
await session._put_inbox("user", envelope)
```

Reyn's session dispatch automatically:
- Surfaces ``sender`` as a state_change history entry (= PR-A
  attribution)
- Captures ``reply_to`` for outbound reply routing (= PR-D2)

## Outbound replies via MCP

The Reyn-side outbox interceptor (= ``reyn.chat.external_routing``)
routes agent replies whose ``reply_to`` is an ``ExternalRef`` through
an MCP tool. Operators configure transport → MCP tool mapping in
``reyn.yaml`` ``external_transports:``:

```yaml
external_transports:
  line:                          # matches ExternalRef.transport
    mcp_tool: line__reply_message
    args_template:
      reply_token: "{destination.reply_token}"
      messages:
        - type: text
          text: "{text}"
```

The webhook plugin doesn't need to handle outbound — Reyn dispatches
via the configured MCP server. Operators install the appropriate
MCP server (= e.g. ``@modelcontextprotocol/server-line``) and
declare the transport mapping.

## Testing

Unit-test the plugin's helpers (= signing verify, envelope mint)
directly. Integration-test the route via FastAPI's ``TestClient``
+ a stubbed ``AgentRegistry`` (= see
``tests/plugins/sample_slack/test_webhook.py`` for the pattern).

Avoid loading the full ``reyn.web.server.app`` in plugin tests; mount
the plugin's router on a fresh ``FastAPI()`` so tests stay
hermetic.

## Conflict resolution

When two installed packages both register the same plugin name (= e.g.
two ``slack`` plugins), the loader logs a warning and uses the first
match. Operators should pin ``package:`` in ``webhooks.yaml`` to
disambiguate.

## Versioning compatibility

Plugins pin their Reyn version range in ``pyproject.toml``:

```toml
dependencies = ["reyn >= 0.1, < 0.2"]
```

The plugin contract (= ``register_router(config) -> APIRouter | None``,
envelope shape, ``ExternalRef`` routing) is intended to stay stable
across minor Reyn versions; breaking changes will be flagged in the
changelog.
