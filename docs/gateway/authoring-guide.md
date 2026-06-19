# Reyn gateway authoring guide

> Gateway framework spec for Reyn integrations
> (= webhook handlers for Slack / LINE / Discord / GitHub etc.).

This guide is for **gateway authors** who want to add inbound webhook
integrations to Reyn (= e.g. a chat-transport adapter for a service
not covered by the bundled samples).

## Design rationale

Reyn core stays free of transport-specific protocol code: signing
schemes, event payload schemas, and SDK dependencies all live in
gateways. The trade-off — operator installs an extra package — is
worth it because:

- Reyn maintainers don't track Slack / LINE / Discord / etc. API drift
- Each transport's SDK choices stay local to its gateway
- Operators run only the gateways they need
- Community can ship gateways independent of Reyn's release cycle

The bundled ``sample_*`` gateways under ``src/reyn/gateway/`` exist as
reference implementations + quick-start fixtures. Production
integrations should fork or replace them.

## Gateway shape

A webhook gateway is a Python package that exposes a single callable
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
``webhooks.yaml`` to activate this gateway.

### register_router contract

```python
from fastapi import APIRouter

def register_router(config: dict) -> APIRouter | None:
    """Build the gateway's webhook router.

    config: per-instance dict from webhooks.yaml, minus reyn-reserved
            keys (package, enabled). Gateway-defined fields only.
    returns: an APIRouter to mount, or None to skip (= e.g. required
             option missing). When returning None, log a warning so
             the operator can see why.
    """
    ...
```

The returned ``APIRouter`` is mounted on the Reyn web app at the path
the gateway chooses. By convention webhook gateways use
``/webhook/<service>`` as the route path so operators paste a
predictable URL into their service's webhook config UI.

## webhooks.yaml schema

Operators activate gateways in ``webhooks.yaml`` (= sibling of
``reyn.yaml`` at the project root):

```yaml
# webhooks.yaml

# Short form: just the gateway name (= empty value)
sample_slack:
  target_agent: news_agent      # gateway-defined field

# Long form: explicit reyn-reserved fields + gateway fields
my_other_gateway:
  package: reyn-gateway-line     # optional: disambiguates same-name gateways
  enabled: false                # optional, default true
  some_option: value            # gateway-defined
```

### Reyn-reserved keys

The loader interprets these and removes them from what's passed to
``register_router``:

| Key | Default | Purpose |
|-----|---------|---------|
| ``package`` | unset | Disambiguates when multiple packages register the same gateway name. Match against the Python distribution name. |
| ``enabled`` | ``true`` | Set ``false`` to deactivate without removing config. |

Gateway authors must avoid using these names in their gateway-defined
fields.

### Per-gateway options

Everything in the per-gateway dict except the reyn-reserved keys is
forwarded to ``register_router`` as the ``config`` argument. The
gateway author defines this schema.

Secrets (= API keys, signing secrets) belong in **environment
variables**, never in webhooks.yaml.

## Stable gateway API — ``reyn.gateway.api``

The module exposes the **stable contract** gateway authors consume.
Internal session methods (= ``_put_inbox`` etc.) may change between
Reyn versions; this API stays stable.

### Helpers

  push_to_agent(*, target_agent, text, sender, reply_to=None,
                kind="user", extra_meta=None, registry=None)
    Deliver a message to a Reyn agent's inbox. Default for webhook
    gateways.

  list_agents(*, registry=None) -> list[str]
    All agent names known to the project (= sorted disk view).
    Use at register_router time to validate config or discover
    targets dynamically.

  agent_exists(name, *, registry=None) -> bool
    Pre-flight check for a single agent name. Defensive: registry
    error → False.

  make_sender(transport, external_id, *, display=None,
              source_scope=None) -> str
    Assemble the documented sender attribution string. Prefer over
    raw f-strings so dispatch attribution label rendering follows
    the standard format. See examples in the docstring.

## Inbound envelope shape + stable gateway API

When the gateway's route receives a webhook, push to the target
agent's inbox via the **stable gateway API** in ``reyn.gateway.api``::

```python
from reyn.runtime.transport import ExternalRef
from reyn.gateway.api import push_to_agent

await push_to_agent(
    target_agent=target_agent,
    text="<message body>",
    sender=f"<transport>:<external_user_id>",
    reply_to=ExternalRef(
        transport="<transport>",       # e.g. "slack", "line"
        destination={                   # transport-specific routing
            "channel": "...",
            "thread_ts": "...",
        },
    ),
)
```

**Do NOT call internal session methods directly.** ``Session.
_put_inbox`` etc. are private API and may change between Reyn
versions; ``reyn.gateway.api`` is the contract that stays stable.

Reyn's session dispatch automatically:
- Surfaces ``sender`` as a state_change history entry (= PR-A
  attribution)
- Captures ``reply_to`` for outbound reply routing (= PR-D2)

The gateway API receives an optional ``registry`` kwarg for tests
(= inject an ``AgentRegistry`` stub). Production code omits it
and uses the process-shared registry.

## Outbound replies via MCP

The Reyn-side outbox interceptor (= ``reyn.runtime.external_routing``)
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

The webhook gateway doesn't need to handle outbound — Reyn dispatches
via the configured MCP server. Operators install the appropriate
MCP server (= e.g. ``@modelcontextprotocol/server-line``) and
declare the transport mapping.

## Testing

Unit-test the gateway's helpers (= signing verify, envelope mint)
directly. Integration-test the route via FastAPI's ``TestClient``
+ a stubbed ``AgentRegistry`` (= see
``tests/gateway/sample_slack/test_webhook.py`` for the pattern).

Avoid loading the full ``reyn.web.server.app`` in gateway tests; mount
the gateway's router on a fresh ``FastAPI()`` so tests stay
hermetic.

## Conflict resolution

When two installed packages both register the same gateway name (= e.g.
two ``slack`` gateways), the loader logs a warning and uses the first
match. Operators should pin ``package:`` in ``webhooks.yaml`` to
disambiguate.

## Versioning compatibility

Gateways pin their Reyn version range in ``pyproject.toml``:

```toml
dependencies = ["reyn >= 0.1, < 0.2"]
```

The gateway contract (= ``register_router(config) -> APIRouter | None``,
envelope shape, ``ExternalRef`` routing) is intended to stay stable
across minor Reyn versions; breaking changes will be flagged in the
changelog.
